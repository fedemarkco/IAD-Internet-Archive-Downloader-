from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from operator import itemgetter
from threading import Thread
from queue import Queue, Empty

import urllib.parse
import threading
import datetime
import requests
import urllib3
import shutil
import time
import math
import sys
import re
import os
import warnings


# Older PyQt5 builds emit this SIP warning when Qt classes are subclassed.
warnings.filterwarnings(
  "ignore",
  message=r"^sipPyTypeDict\(\) is deprecated, the extension module should use sipPyTypeDictRef\(\) instead$",
  category=DeprecationWarning,
  module=rf"^{re.escape(__name__)}$",
)

USER_AGENT = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"}
MAX_THREADS = 100

sem = threading.Semaphore()
urllib3.disable_warnings()

class ArchiveOrg(QObject):
  _response = pyqtSignal(object)
  _titles = pyqtSignal(list)
  _ql = pyqtSignal(list)
  _size = pyqtSignal(Queue)

  def __init__(self, user=None, password=None, url=None, cookies=None, search_results=None, parent=None):
    super(ArchiveOrg, self).__init__(parent)
    self.url = url
    self.user = user
    self.password = password
    self.cookies = cookies
    self.search_results = search_results
    self.directory_initialized = 0
    self.file_queue = Queue()
    self.file_list = []
    self.path_d = os.path.dirname(__file__) + '\\'
    self.total_threads = MAX_THREADS
    self.user_agent = USER_AGENT

  def login(self):
    url_login = "https://archive.org/services/account/login/"
    url_csrf_token = "https://archive.org/services/csrf-token"
    response = requests.get(url_csrf_token, headers=self.user_agent)
    csrf_token = response.json()["value"]["token"]
    source_post = requests.post(
      url_login, 
      headers={
        **self.user_agent,
        "x-csrf-token": csrf_token,
      },
      cookies=response.cookies,
      json = {
        "username": self.user, 
        "password": self.password,
        "remember": "true", 
        "t": csrf_token
      }
    )
    self._response.emit(source_post)

  def strip_tags(self, value):
    return re.compile(r'<[^<]*?/?>').sub("", value)

  def get_file_size(self):
    q = self.search_results
    out = Queue()
    num_threads = min(self.total_threads, q.qsize())
    for i in range(num_threads):
      worker = Thread(target=self.get_file_size_worker, args=(q, out,))
      worker.daemon = True
      worker.start()

    q.join()

    self._size.emit(out)

  def get_file_size_response(self, u):
    max_retries = 5

    for attempt in range(max_retries):
      try:
        with requests.get(
          u[2],
          cookies=self.cookies,
          stream=True,
          headers=self.user_agent,
          timeout=(10, 60)
        ) as response:
          response.raise_for_status()
          length = response.headers.get("Content-Length")
          return (u[0], u[1], u[2], int(length) if length is not None else None, u[3])

      except requests.HTTPError as error:
        if error.response.status_code not in (429, 500, 502, 503, 504):
          break

      except (requests.RequestException, ValueError):
        pass

      if attempt < max_retries - 1:
        time.sleep(2 ** attempt)

    return (u[0], u[1], u[2], None, u[3])

  def get_file_size_worker(self, q, out):
    while True:
      try:
        u = q.get_nowait()
      except Empty:
        break

      try:
        result = self.get_file_size_response(u)

        if result[3] is not None:
          out.put(result)

      except Exception as error:
        pass

      finally:
        q.task_done()

  def get_titles(self):
    source = requests.get(self.url, cookies=self.cookies, headers=self.user_agent)
    search_results = re.findall(r'<tr.*?>\s+<td><a\shref="(.*?)">(.*?)<\/a>', source.text)[1:]
    self._titles.emit(search_results)

  def get_directory(self):
    self.get_directory_recursive(self.search_results, self.cookies, self.url)
    self._ql.emit(self.file_list)

  def get_directory_recursive(self, search_results, cookies, url_temp='', folders=''):
    source = requests.get(url_temp, cookies=cookies, headers=self.user_agent)
    if self.directory_initialized == 0:
      self.directory_initialized = 1
      search_temp = []
      for m in search_results:
        if m[1][-1] != "/":
          if url_temp[-1] != "/":
            url_temp += "/"
          self.file_list.append((url_temp+m[0], '', m[1], cookies))
        else:
          search_temp.append(m)
      search_results = search_temp
    else:
      search_results = re.findall(r'<tr.*?>\s+<td><a\shref="(.*?)">(.*?)<\/a>', source.text)

    for d in range(len(search_results)):
      if "to parent directory" in search_results[d][1]:
        continue
      u = search_results[d][0] #url
      f = search_results[d][1] #filename
      if url_temp[-1] != "/":
        url_temp += "/"
      if f[-1] == "/":
        self.get_directory_recursive(search_results, cookies, url_temp+u, folders + f[:-1] + '\\')
      else:
        files = re.findall(r'<tr.*?>\s+<td><a\shref="(.*?)">.*?<\/a>', source.text)[1:]
        for f in files:
          if (url_temp+f, folders, f, cookies) in self.file_list:
            continue
          self.file_list.append((url_temp+f, folders, f, cookies))

class Downloader(QThread):
  progress_updated = pyqtSignal(list)
  status_updated = pyqtSignal(int, int)
  _finished = pyqtSignal()

  def __init__(self, cookies, threads, info_files, dest, parent=None):
    super(Downloader, self).__init__(parent)
    self.cookies = cookies
    self.num_threads = threads
    self.info_files = info_files
    self.dest = dest
    self.chunk_size = 512000
    self.speed_file = []
    self.total_threads = MAX_THREADS
    self.download_semaphore = threading.BoundedSemaphore(self.total_threads)
    self.progress_lock = threading.Lock()
    self.user_agent = USER_AGENT

  def run(self):
    self.file_list = []
    self.input_queue = Queue()

    for l in self.info_files:
      self.input_queue.put(l)

    self.num_threads = min (self.num_threads, len(self.info_files))
    for i in range(self.num_threads):
      worker = Thread(target=self.download_thread, args=(self.input_queue,))
      worker.start()

    self.input_queue.join()
    self._finished.emit()

    if os.path.isdir("__pycache__"):
      shutil.rmtree("__pycache__")

  def update_download_progress(self, index, fragment_start, downloaded_bytes, filesize, start):
    with self.progress_lock:
      state = next(item for item in self.speed_file if item["id"] == index)

      state["active_fragments"][fragment_start] = downloaded_bytes

      now = time.perf_counter()

      if now - state["last_update"] < 0.25:
        return

      state["last_update"] = now

      total_bytes = state["speed"] + sum(state["active_fragments"].values())
      elapsed = max(now - start, 0.001)

      speed = total_bytes / elapsed
      remaining = max(0, (filesize - total_bytes) / speed) if filesize and speed > 0 else None

      progress = int(total_bytes * 100 / filesize) if filesize else 0
      progress = min(progress, 100)

      self.progress_updated.emit([
        total_bytes,
        round(elapsed),
        speed,
        progress,
        index,
        "Downloading",
        round(remaining) if remaining is not None else None
      ])

  def download_worker(self, q, url, dest, filesize, index, start, start_time):
    while True:
      try:
        a, b, filename = q.get_nowait()
      except Empty:
        return

      try:
        success = False

        for attempt in range(3):
          try:
            with self.download_semaphore:
              fragment_path = self.get_file(
                a, b, filename, url, dest, filesize, index, start, start_time
              )

            with self.progress_lock:
              state = next(item for item in self.speed_file if item["id"] == index)
              state["body"].append((a, b, fragment_path))

            success = True
            break

          except (requests.RequestException, OSError) as error:
            if attempt < 2:
              time.sleep(2 ** attempt)

        if not success:
          with self.progress_lock:
            state = next(item for item in self.speed_file if item["id"] == index)
            state["failed"] = True

      finally:
        q.task_done()

  def create_file(self, dest, filename, index):
    state = next(item for item in self.speed_file if item["id"] == index)
    fragments = sorted(state["body"], key=lambda item: item[0])

    self.status_updated.emit(index, 1)

    output_path = os.path.join(dest, filename)
    temporary_path = output_path + ".building"

    with open(temporary_path, "wb") as output:
      for a, b, fragment_path in fragments:
        with open(fragment_path, "rb") as source:
          shutil.copyfileobj(source, output)

    os.replace(temporary_path, output_path)

    for a, b, fragment_path in fragments:
      os.remove(fragment_path)

    self.status_updated.emit(index, 2)

  def write_fragment(self, dest, filename, fragment):
    f = open(os.path.join(dest, filename), "wb")
    f.write(fragment)
    f.close()

  def get_file(self, a, b, filename, url, dest, filesize, index, start, start_time):
    fragment_path = os.path.join(dest, f"{filename}-{a}-{b}.part")
    temporary_path = fragment_path + ".tmp"

    headers = dict(self.user_agent)

    if filesize is not None:
      headers["Range"] = f"bytes={a}-{b}"

    try:
      with requests.get(
        url,
        headers=headers,
        cookies=self.cookies,
        stream=True,
        timeout=(10, 60)
      ) as response:
        response.raise_for_status()

        if filesize is not None:
          if response.status_code != 206:
            raise requests.RequestException("Server did not honor the requested range")

          expected_range = f"bytes {a}-{b}/{filesize}"
          if response.headers.get("Content-Range") != expected_range:
            raise requests.RequestException("Unexpected Content-Range")

        downloaded_bytes = 0

        with open(temporary_path, "wb") as output:
          for chunk in response.iter_content(chunk_size=self.chunk_size):
            if not chunk:
              continue

            output.write(chunk)
            downloaded_bytes += len(chunk)

            self.update_download_progress(
              index, a, downloaded_bytes, filesize, start
            )

        if filesize is not None and downloaded_bytes != b - a + 1:
          raise requests.RequestException("Incomplete fragment")

        os.replace(temporary_path, fragment_path)

      with self.progress_lock:
        state = next(item for item in self.speed_file if item["id"] == index)
        state["speed"] += downloaded_bytes
        state["active_fragments"].pop(a, None)

      return fragment_path

    except Exception:
      with self.progress_lock:
        state = next(item for item in self.speed_file if item["id"] == index)
        state["active_fragments"].pop(a, None)
      if os.path.exists(temporary_path):
        os.remove(temporary_path)
      raise

  def prepare_download(self, filename, dest, url, filesize, index):
    if not filename.strip():
      filename = os.path.basename(urllib.parse.urlparse(url).path)

    os.makedirs(dest, exist_ok=True)

    state = {
      "id": index,
      "url": url,
      "filename": filename,
      "speed": 0,
      "body": [],
      "failed": False,
      "active_fragments": {},
      "last_update": 0
    }

    with self.progress_lock:
      self.speed_file.append(state)

    q = Queue()

    if filesize is not None:
      if filesize <= 0:
        self.status_updated.emit(index, 3)
        return

      for a in range(0, filesize, 3000000):
        b = min(a + 3000000, filesize) - 1
        q.put((a, b, filename))
    else:
      q.put((0, 0, filename))

    start = time.perf_counter()
    start_time = time.time()

    worker_count = min(self.total_threads, q.qsize())
    workers = []

    for i in range(worker_count):
      worker = Thread(
        target=self.download_worker,
        args=(q, url, dest, filesize, index, start, start_time)
      )
      worker.start()
      workers.append(worker)

    for worker in workers:
      worker.join()

    if state["failed"]:
      self.status_updated.emit(index, 3)
      return

    self.create_file(dest, filename, index)

  def download_thread(self, q):
    while not q.empty():
        u = q.get()
        self.prepare_download(
          urllib.parse.unquote(u[0]), 
          os.path.join(self.dest, u[1]), 
          u[2], 
          u[3], 
          u[4])
        q.task_done()

class ChecklistDialog(QDialog):
  def __init__(self, parent=None):
    super(ChecklistDialog, self).__init__(parent)
    self.parent = parent
    self.button_pressed = False
    self.name = self.parent.name
    self.icon = None
    self.model = QStandardItemModel()
    self.list_view = QListView()
    string_list = self.parent.list_files
    checked = True

    for string in string_list:
      item = QStandardItem(string[1])
      item.setEditable(False)
      item.setCheckable(True)
      check = \
        (Qt.Checked if checked else Qt.Unchecked)
      item.setCheckState(check)
      self.model.appendRow(item)

    self.list_view.setModel(self.model)
    self.model.itemChanged.connect(self.on_item_changed)
    self.ok_button = QPushButton("OK")
    self.select_button = QPushButton("Select All")
    self.unselect_button = QPushButton("Unselect All")

    hbox = QHBoxLayout()
    hbox.addStretch(1)
    hbox.addWidget(self.ok_button)
    hbox.addWidget(self.select_button)
    hbox.addWidget(self.unselect_button)

    vbox = QVBoxLayout(self)
    vbox.addWidget(self.list_view)
    vbox.addStretch(1)
    vbox.addLayout(hbox)

    self.setWindowTitle(self.name)

    if self.icon:
      self.setWindowIcon(self.icon)

    self.ok_button.clicked.connect(self.on_accepted)
    self.select_button.clicked.connect(self.select)
    self.unselect_button.clicked.connect(self.unselect)

    self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

    self.setFixedSize(280, 260)

  def strip_tags(self, value):
    return re.compile(r'<[^<]*?/?>').sub("", value)

  def on_accepted(self):
    self.choices = [self.model.item(i).text() for i in
                    range(self.model.rowCount())
                    if self.model.item(i).checkState()
                    == Qt.Checked]
    self.accept()

  def on_item_changed(self, item):
    if self.button_pressed:
      self.list_view.setCurrentIndex(item.index())

  def select(self):
    self.button_pressed = False
    for i in range(self.model.rowCount()):
      item = self.model.item(i)
      item.setCheckState(Qt.Checked)
    self.button_pressed = True

  def unselect(self):
    self.button_pressed = False
    for i in range(self.model.rowCount()):
      item = self.model.item(i)
      item.setCheckState(Qt.Unchecked)
    self.button_pressed = True

class WLogin(QDialog):
  def __init__(self, parent=None):
    super(WLogin, self).__init__(parent)

    self.setFixedWidth(600)
    self.setFixedHeight(80)

    self.message = QLabel("")
    self.message.setStyleSheet("color: red;")

    self.user = QLineEdit("")
    self.user.setPlaceholderText("user")
    self.password_input = QLineEdit("")
    self.password_input.setPlaceholderText("password")
    self.password_input.setEchoMode(QLineEdit.Password)
    self.button_login = QPushButton("Login")

    log = QHBoxLayout()
    log.addWidget(self.user)
    log.addWidget(self.password_input)
    log.addWidget(self.button_login)

    message_widget = QVBoxLayout()
    message_widget.addWidget(self.message)

    login_layout = QGridLayout()
    login_layout.addLayout(log, 0, 0, 5, 78)
    login_layout.addLayout(message_widget, 6, 31, 1, 10)
    self.setLayout(login_layout)

    self.button_login.clicked.connect(self.login)

    self.setWindowTitle("IAD (Internet Archive Downloader) - Login")

    self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)

    QApplication.setStyle(QStyleFactory.create("Fusion"))

  def login(self):
    self.button_login.setEnabled(False)
    self.message.setText("Verifying...")
    self.message.repaint()
    self._archive_org = ArchiveOrg(self.user.text(), self.password_input.text(), None, None, None)
    thread = QThread(self)
    self._archive_org.moveToThread(thread)
    self._archive_org._response.connect(self.login_result)
    thread.started.connect(self._archive_org.login)
    thread.start()
  
  @pyqtSlot(object)
  def login_result(self, response):
    message = response.json()
    self.cookies = response.cookies
    if message.get("success", None):
      self.message.setText("Success")
      self.message.repaint()
      self.accept()
      _main = Main(self)
      _main.show()
    else:
      self.message.setText("Failed")
      self.message.repaint()
    self.button_login.setEnabled(True)

class ProgressBar(QProgressBar):
  def __init__(self, value, parent=None):
    super().__init__(parent)
    self.setMinimum(0)
    self.setMaximum(100)
    self.setValue(value)

class Main(QDialog):
  def __init__(self, parent=None):
    super(Main, self).__init__(parent)

    self.parent = parent

    self.cookies = self.parent.cookies

    self.setFixedWidth(600)
    self.setFixedHeight(600)

    self.info_files = []
    self.count = 0
    self.finished = False

    self.url_line = QLineEdit("")

    url_label = QLabel("&URL:")
    url_label.setBuddy(self.url_line)

    self.button_search = QPushButton("Search")

    self.searching_label = QLabel("")
    self.searching_label.setStyleSheet("color: red;")

    search_label_layout = QVBoxLayout()
    search_label_layout.addWidget(self.searching_label)

    thread_label = QLabel("&Thread:")
    self.thread_combo_box = QComboBox()
    self.thread_combo_box.addItems(["1", "2", "4", "8", "16", "32"])
    thread_label.setBuddy(self.thread_combo_box)

    self.folder_path_label = QLabel("")

    button_folder = QPushButton("Save To Folder")

    self.button_start = QPushButton("Start")
    self.button_start.setEnabled(False)
    self.button_clean = QPushButton("Clean")
    button_exit = QPushButton("Exit")

    self.create_table()

    table = QVBoxLayout()
    table.addWidget(self.table_widget)

    top_layout_2 = QHBoxLayout()
    top_layout_2.addWidget(thread_label)
    top_layout_2.addWidget(self.thread_combo_box)
    top_layout_2.addWidget(button_folder)

    top_layout = QHBoxLayout()
    top_layout.addWidget(url_label)
    top_layout.addWidget(self.url_line)
    top_layout.addWidget(self.button_search)

    top_layout_3 = QHBoxLayout()
    top_layout_3.addWidget(self.folder_path_label)

    button_start_clean = QHBoxLayout()
    button_start_clean.addWidget(self.button_start)
    button_start_clean.addWidget(self.button_clean)

    button_exit_layout = QHBoxLayout()
    button_exit_layout.addWidget(button_exit)

    main_layout = QGridLayout()
    main_layout.addLayout(top_layout, 0, 0, 2, 78)
    top_layout.setContentsMargins(0, 15, 0, 40)
    main_layout.addLayout(search_label_layout, 1, 30, 1, 10)
    main_layout.addLayout(top_layout_2, 2, 0, 1, 1)
    main_layout.addLayout(top_layout_3, 2, 1, 1, 70)
    main_layout.addLayout(table, 3, 0, 1, 78)
    main_layout.addLayout(button_start_clean, 4, 0, 1, 30)
    main_layout.addLayout(button_exit_layout, 4, 63, 1, 15)
    table.setContentsMargins(0, 30, 0, 0)
    self.setLayout(main_layout)

    self.button_search.clicked.connect(self.search)
    button_folder.clicked.connect(self.select_folder)
    self.button_clean.clicked.connect(self.select_clean)
    self.button_start.clicked.connect(self.select_start)
    button_exit.clicked.connect(self.close)

    self.setWindowTitle("IAD (Internet Archive Downloader)")

    self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)

    QApplication.setStyle(QStyleFactory.create("Fusion"))

  def mousePressEvent(self, e):
    self.table_widget.clearSelection()

  def get_size_format(self, b, factor=1024, suffix="B"):
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
      if b < factor:
        return f"{b:.2f} {unit}{suffix}"
      b /= factor
    return f"{b:.2f}Y {suffix}"

  @pyqtSlot(list)
  def insert_progress_bar_table(self, values):
    self.update_row(*values)

  def check_url(self, url):
    if len(url.strip()) == 0:
      return "V"
    if not url.startswith("https://archive.org/download/"):
      return "E"
    else:
      if url[-1] != "/":
        url += "/"
      if len(url.replace("https://archive.org/download/", "").split("/")) == 2:
        return True
      else:
        return False

  def message_search(self, title, text):
    msg = QMessageBox(self)
    msg.setWindowTitle(title)
    msg.setIcon(QMessageBox.Warning)
    msg.setText(text)
    msg.exec_()

  def search(self):
    _type = self.check_url(self.url_line.text())
    if isinstance(_type, str):
      if _type == "V":
        self.message_search("Wrong url", "The URL cannot be empty")
      if _type == "E":
        self.message_search("Wrong url", "The URL should be as follows:\nhttps://archive.org/download/example")
    if isinstance(_type, bool):
      if not _type:
        self.message_search("Wrong url", "The URL should be as follows:\nhttps://archive.org/download/example")
      else:
        self.button_search.setEnabled(False)
        self.searching_label.setText("Searching...")
        self.searching_label.repaint()
        self._archive_org = ArchiveOrg(None, None, self.url_line.text(), self.cookies, None)
        thread = QThread(self)
        self._archive_org.moveToThread(thread)
        self._archive_org._titles.connect(self.search_step_1)
        thread.started.connect(self._archive_org.get_titles)
        thread.start()

  @pyqtSlot(list)
  def search_step_1(self, list_files):
    search_results = []
    self.info_files = []
    self.list_files = list_files
    self.name = "Found"
    self.searching_label.setText("")
    self.searching_label.repaint()
    self.check_list = ChecklistDialog(self)
    url = self.url_line.text()

    if url[-1] != "/":
      url += "/"

    if self.check_list.exec_() == QDialog.Accepted:
      self.select_clean()
      for s in self.check_list.choices:
        for k in self.list_files:
          if k[1] == s:
            search_results.append(k)
      if len(search_results) > 0:
        self.searching_label.setText("Getting links...")
        self.searching_label.repaint()
        self._archive_org = ArchiveOrg(None, None, self.url_line.text(), self.cookies, search_results)
        thread = QThread(self)
        self._archive_org.moveToThread(thread)
        self._archive_org._ql.connect(self.search_step_2)
        thread.started.connect(self._archive_org.get_directory)
        thread.start()

    self.button_search.setEnabled(True)

  @pyqtSlot(list)
  def search_step_2(self, ql):
    q = Queue()
    index = 0

    for u in ql:
      url_file = u[0]
      dest = u[1]
      filename = u[2]
      q.put((filename, dest, url_file, index))
      index += 1

    self._archive_org = ArchiveOrg(None, None, None, self.cookies, q)
    thread = QThread(self)
    self._archive_org.moveToThread(thread)
    self._archive_org._size.connect(self.search_step_3)
    thread.started.connect(self._archive_org.get_file_size)
    thread.start()

  def refresh_progress_bar(self):
    if len(self.info_files) > 0:
      progress_value = self.table_widget.cellWidget(0, 5).value()
      self.table_widget.removeCellWidget(0, 5)
      progress_bar = ProgressBar(progress_value)
      self.table_widget.setCellWidget(0, 5, progress_bar)
  
  @pyqtSlot(Queue)
  def search_step_3(self, out):
    if out.qsize() <= 15:
      self.table_widget.setRowCount(15)
      for row in range(self.table_widget.rowCount()):
        self.table_widget.setRowHeight(row, 10)
    else:
      self.table_widget.setRowCount(out.qsize())

    size = out.qsize()

    while not out.empty():
      res = out.get()
      if res[3] is not None:
        self.add_row_table(res[0], self.get_size_format(res[3]), res[4])
      else:
        self.add_row_table(res[0], '-', res[4])
      self.info_files.append((res[0], res[1], res[2], res[3], res[4]))

    self.searching_label.setText("")
    self.searching_label.repaint()

    self.setWindowTitle("IAD (Internet Archive Downloader) - 0/{0} files".format(size))

    self.button_start.setEnabled(True)
    self.finished = False

  def hide_login(self):
    self.my_widget.setVisible(False)

  def select_start(self):
    if self.folder_path_label.text() == "":
      msg = QMessageBox(self)
      msg.setWindowTitle("Information")
      msg.setIcon(QMessageBox.Warning)
      msg.setText("You must select the folder where\nthe downloaded files will be saved.")
      msg.exec_()
    else:

      if self.finished:
        self.zero_progress()

      self.info_files = sorted(self.info_files, key=itemgetter(2))
      self._downloader = Downloader(
        self.cookies, 
        int(self.thread_combo_box.currentText()), 
        self.info_files, 
        self.folder_path_label.text(), 
        self
      )
      self._downloader.progress_updated.connect(self.insert_progress_bar_table)
      self._downloader.status_updated.connect(self.update_status)
      self._downloader.start()
      self.button_clean.setEnabled(False)
      self.button_search.setEnabled(False)
      self.button_start.setEnabled(False)
      self._downloader._finished.connect(self.button_start_finished)

      self.count = 0
      self.finished = True

  @pyqtSlot()
  def button_start_finished(self):
    self.button_search.setEnabled(True)
    self.button_start.setEnabled(True)
    self.button_clean.setEnabled(True)

  def zero_progress(self):
    size = len(self.info_files)
    self.setWindowTitle("IAD (Internet Archive Downloader) - 0/{0} files".format(size))

    for index in range(len(self.info_files)):
      self.table_widget.setItem(index, 1, QTableWidgetItem(""))
      size = re.search(r'.*?\/(.*)', self.table_widget.item(index, 2).text())
      size = size.group(1).strip()
      self.table_widget.setItem(index, 2, QTableWidgetItem("0 / "+size))
      self.table_widget.setItem(index, 3, QTableWidgetItem(""))
      self.table_widget.setItem(index, 4, QTableWidgetItem(""))
      progress_bar = self.table_widget.cellWidget(index, 5)
      progress_bar.setValue(0)

  def select_clean(self):
    self.info_files.clear()
    self.table_widget.clearContents()
    self.table_widget.setRowCount(15)
    self.button_start.setEnabled(False)

  def select_folder(self):
    dialog = QFileDialog(self)
    folder_path = dialog.getExistingDirectory(self, "Select Folder")
    if self.folder_path_label.text() != '' and folder_path == '':
      folder_path = self.folder_path_label.text()
    if len(folder_path) > 60:
      folder_path = folder_path[:60]+'...'
    self.folder_path_label.setText(folder_path)

  def add_row_table(self, filename, size, index):
    filename = urllib.parse.unquote(filename)
    self.table_widget.setItem(index, 0, QTableWidgetItem(filename))

    if size != '':
      self.table_widget.setItem(index, 2, QTableWidgetItem("0 / "+size))
    else:
      self.table_widget.setItem(index, 2, QTableWidgetItem("-"))

    progress_bar = ProgressBar(0)

    self.table_widget.setCellWidget(index, 5, progress_bar)
    self.table_widget.setRowHeight(index, 10)

  def create_progress_bar(self, _max):
    self.progress_bar = QProgressBar()
    self.progress_bar.setRange(0, _max)
    self.progress_bar.setValue(0)
    return self.progress_bar

  def create_table(self):
    self.table_view = QHeaderView(Qt.Horizontal)
    self.table_view.sectionResized.connect(self.refresh_progress_bar)
    self.table_widget = QTableWidget()
    self.setFixedHeight(496)
    self.table_widget.setRowCount(15)
    self.table_widget.setColumnCount(6)
    self.table_widget.setHorizontalHeaderLabels(
      [
        "Filename", 
        "Status", 
        "Uploaded / Size", 
        "Time / Remaining", 
        "Speed", 
        "Progress"
      ]
    )
    self.table_widget.verticalHeader().setVisible(False)

    for row in range(self.table_widget.rowCount()):
      self.table_widget.setRowHeight(row, 10)

    self.table_widget.setHorizontalHeader(self.table_view)
    self.table_widget.horizontalHeader().setStretchLastSection(True)
    self.table_widget.setColumnWidth(1, 78)
    self.table_widget.setColumnWidth(2, 110)
    self.table_widget.setColumnWidth(3, 105)
    self.table_widget.setColumnWidth(4, 75)
    self.table_widget.setEditTriggers(self.table_widget.NoEditTriggers)
    
  @pyqtSlot(int, int)
  def update_status(self, index, option):
    if option == 1:
      self.table_widget.setItem(index, 1, QTableWidgetItem("Building File"))
    if option == 2:
      progress_bar = self.table_widget.cellWidget(index, 5).setValue(100)
      self.table_widget.setItem(index, 1, QTableWidgetItem("Done"))
      self.count += 1
      title = self.windowTitle()
      title = re.sub(r' - \d+/', f' - {self.count}/', title)
      self.setWindowTitle(title)
    if option == 3:
      self.table_widget.setItem(index, 1, QTableWidgetItem("Failed"))

  def update_row(self, upload_size, _time, speed, progress, index, status, remaining):
    self.table_widget.setItem(index, 1, QTableWidgetItem(status))

    size = self.table_widget.item(index, 2).text().split("/")[1].strip()
    self.table_widget.setItem(index, 2, QTableWidgetItem(self.get_size_format(upload_size) + " / " + size))

    elapsed_text = str(datetime.timedelta(seconds=_time))
    remaining_text = str(datetime.timedelta(seconds=remaining)) if remaining is not None else "--:--:--"

    self.table_widget.setItem(index, 3, QTableWidgetItem(f"{elapsed_text} / {remaining_text}"))

    if speed != "-":
      self.table_widget.setItem(index, 4, QTableWidgetItem(self.get_size_format(speed) + "/s"))
    else:
      self.table_widget.setItem(index, 4, QTableWidgetItem(speed))

    progress_bar = self.table_widget.cellWidget(index, 5)
    progress_bar.setValue(progress)

if __name__ == "__main__":
  app = QApplication([])
  gallery = WLogin()
  gallery.show()
  sys.exit(app.exec_())
