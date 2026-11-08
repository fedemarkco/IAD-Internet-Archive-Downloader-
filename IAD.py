from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from operator import itemgetter
from threading import Thread
from queue import Queue, Empty

import urllib.parse
from html import unescape
import threading
import logging
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
  search_error = pyqtSignal(str)
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
    self.search_cancelled = threading.Event()
    self.file_queue = Queue()
    self.file_list = []
    self.path_d = os.path.dirname(__file__) + '\\'
    self.total_threads = MAX_THREADS
    self.user_agent = USER_AGENT

  def login(self):
    url_login = "https://archive.org/services/account/login/"
    url_csrf_token = "https://archive.org/services/csrf-token"
    try:
      response = requests.get(url_csrf_token, headers=self.user_agent, timeout=(10, 30))
      response.raise_for_status()
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
        },
        timeout=(10, 30)
      )
    except (requests.RequestException, ValueError, KeyError, TypeError):
      source_post = None
    self._response.emit(source_post)

  def strip_tags(self, value):
    return re.compile(r'<[^<]*?/?>').sub("", value)

  def get_file_size(self):
    q = self.search_results
    out = Queue()
    num_threads = min(self.total_threads, q.qsize())
    workers = [
      Thread(target=self.get_file_size_worker, args=(q, out))
      for _ in range(num_threads)
    ]
    for worker in workers:
      worker.start()
    for worker in workers:
      worker.join()

    self._size.emit(out)

  def get_file_size_response(self, u):
    max_retries = 5

    for attempt in range(max_retries):
      if self.search_cancelled.is_set():
        break
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
        self.search_cancelled.wait(2 ** attempt)

    return (u[0], u[1], u[2], None, u[3])

  def get_file_size_worker(self, q, out):
    while not self.search_cancelled.is_set():
      try:
        u = q.get_nowait()
      except Empty:
        break

      try:
        result = self.get_file_size_response(u)

        if result[3] is not None:
          out.put(result)

      except Exception:
        pass

      finally:
        q.task_done()

  def directory_links(self, markup):
    links = re.findall(r'<tr.*?>\s+<td><a\shref="(.*?)">(.*?)<\/a>', markup)
    return [(unescape(href), unescape(name)) for href, name in links]

  def get_titles(self):
    try:
      source = requests.get(self.url, cookies=self.cookies, headers=self.user_agent, timeout=(10, 30))
      source.raise_for_status()
    except requests.RequestException as error:
      self.search_error.emit(str(error))
      return
    search_results = self.directory_links(source.text)[1:]
    self._titles.emit(search_results)

  def get_directory(self):
    try:
      self.get_directory_recursive(self.search_results, self.cookies, self.url)
    except requests.RequestException as error:
      self.search_error.emit(str(error))
      return
    self._ql.emit(self.file_list)

  def get_directory_recursive(self, search_results, cookies, url_temp='', folders=''):
    if self.search_cancelled.is_set():
      return
    source = requests.get(url_temp, cookies=cookies, headers=self.user_agent, timeout=(10, 30))
    source.raise_for_status()
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
      search_results = self.directory_links(source.text)

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
        files = [href for href, _ in search_results[1:]]
        for f in files:
          if (url_temp+f, folders, f, cookies) in self.file_list:
            continue
          self.file_list.append((url_temp+f, folders, f, cookies))

class FragmentPaused(Exception):
  pass


class Downloader(QThread):
  progress_updated = pyqtSignal(list)
  status_updated = pyqtSignal(int, int)
  download_error = pyqtSignal(int, str)
  pause_changed = pyqtSignal(int)
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
    self.resume_event = threading.Event()
    self.resume_event.set()
    self.schedule = threading.Condition()
    self.stopping = False
    self.pending = list(info_files)
    self.unfinished = {info[4] for info in info_files}
    self.active_files = set()
    self.paused_files = set()
    self.resumed_files = set()
    self.assembling = set()
    self.file_states = {}
    self.fragment_workers = {}

  def request_stop(self):
    with self.schedule:
      self.stopping = True
      self.schedule.notify_all()

  def set_paused(self, paused):
    with self.schedule:
      if paused:
        self.resume_event.clear()
      else:
        self.resume_event.set()
        self.paused_files.clear()
      self.resumed_files.clear()
      indexes = tuple(self.unfinished)
      self.schedule.notify_all()
    for index in indexes:
      self.pause_changed.emit(index)

  def set_file_paused(self, index, paused):
    with self.schedule:
      if index not in self.unfinished or index in self.assembling:
        return
      if paused:
        self.paused_files.add(index)
        self.resumed_files.discard(index)
      else:
        self.paused_files.discard(index)
        if not self.resume_event.is_set():
          self.resumed_files.add(index)
      self.schedule.notify_all()
    self.pause_changed.emit(index)

  def is_file_paused(self, index):
    return index in self.paused_files or (
      not self.resume_event.is_set() and index not in self.resumed_files
    )

  def pause_summary(self):
    with self.schedule:
      pending = self.unfinished - self.assembling
      paused = {index for index in pending if self.is_file_paused(index)}
      return bool(paused), bool(pending - paused), bool(paused & self.active_files)

  def file_pause_state(self, index):
    with self.schedule:
      available = index in self.unfinished and index not in self.assembling
      individual = index in self.paused_files
      paused = available and self.is_file_paused(index)
      return available, individual, paused, index in self.active_files

  def run(self):
    self.num_threads = min(self.num_threads, len(self.info_files))
    workers = [Thread(target=self.download_thread) for _ in range(self.num_threads)]
    for worker in workers:
      worker.start()
    for worker in workers:
      worker.join()
    self._finished.emit()

  def update_download_progress(self, index, fragment_start, downloaded_bytes, filesize, start):
    with self.progress_lock:
      state = next(item for item in self.speed_file if item["id"] == index)

      if fragment_start is not None:
        state["active_fragments"][fragment_start] = downloaded_bytes

      now = time.perf_counter()

      if fragment_start is not None and now - state["last_update"] < 0.25:
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

  def download_worker(self, session, q, url, dest, filesize, index, start, start_time):
    while True:
      with self.schedule:
        if (self.stopping or self.is_file_paused(index)
            or self.fragment_workers[index] > self.fragment_quota()):
          return
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
                session, a, b, filename, url, dest, filesize, index, start, start_time
              )

            with self.progress_lock:
              state = next(item for item in self.speed_file if item["id"] == index)
              state["body"].append((a, b, fragment_path))

            success = True
            break

          except FragmentPaused:
            q.put((a, b, filename))
            return
          except (requests.RequestException, OSError) as error:
            if attempt < 2:
              with self.schedule:
                if self.schedule.wait_for(lambda: self.stopping, timeout=2 ** attempt):
                  return
            else:
              self.download_error.emit(index, str(error))

        if not success:
          with self.progress_lock:
            state = next(item for item in self.speed_file if item["id"] == index)
            state["failed"] = True

      except Exception as error:
        self.download_error.emit(index, str(error))
        logging.exception("Unexpected fragment download failure for %s", filename)
        with self.progress_lock:
          state = next(item for item in self.speed_file if item["id"] == index)
          state["failed"] = True
      finally:
        q.task_done()

  def create_file(self, dest, filename, index, filesize):
    state = next(item for item in self.speed_file if item["id"] == index)
    fragments = sorted(state["body"], key=lambda item: item[0])

    self.status_updated.emit(index, 1)

    output_path = os.path.join(dest, filename)
    temporary_path = output_path + ".building"

    if not fragments:
      raise OSError("No fragments were downloaded")

    try:
      with open(temporary_path, "wb") as output:
        offset = 0
        for first, last, fragment_path in fragments:
          if filesize is not None and first != offset:
            raise OSError("Missing or overlapping download fragments")
          with open(fragment_path, "rb") as source:
            shutil.copyfileobj(source, output)
          offset = output.tell()
          if filesize is not None and offset != last + 1:
            raise OSError("Incorrect fragment size")
        if offset == 0 or (filesize is not None and offset != filesize):
          raise OSError("Downloaded file size does not match the expected size")

      os.replace(temporary_path, output_path)
    finally:
      if os.path.exists(temporary_path):
        os.remove(temporary_path)

    for _, _, fragment_path in fragments:
      try:
        os.remove(fragment_path)
      except OSError:
        logging.warning("Could not remove temporary fragment %s", fragment_path)

    self.status_updated.emit(index, 2)

  def write_fragment(self, dest, filename, fragment):
    f = open(os.path.join(dest, filename), "wb")
    f.write(fragment)
    f.close()

  def get_file(self, session, a, b, filename, url, dest, filesize, index, start, start_time):
    fragment_path = os.path.join(dest, f"{filename}-{a}-{b}.part")
    temporary_path = fragment_path + ".tmp"

    state = next(item for item in self.speed_file if item["id"] == index)
    with self.progress_lock:
      saved_bytes, validator = state["partial"].get(a, (0, None))
    downloaded_bytes = saved_bytes
    headers = dict(self.user_agent)
    range_start = a + saved_bytes
    if filesize is not None:
      headers["Range"] = f"bytes={range_start}-{b}"
      if validator:
        headers["If-Range"] = validator

    try:
      with self.schedule:
        if self.stopping or self.is_file_paused(index):
          raise FragmentPaused

      with session.get(
        url,
        headers=headers,
        cookies=self.cookies,
        stream=True,
        timeout=(10, 60)
      ) as response:
        response.raise_for_status()

        if filesize is not None:
          if validator and response.status_code == 200:
            raise ValueError("The server refused to resume the validated fragment")
          if response.status_code != 206:
            raise requests.RequestException("Server did not honor the requested range")
          if response.headers.get("Content-Range") != f"bytes {range_start}-{b}/{filesize}":
            raise requests.RequestException("Unexpected Content-Range")

        etag = response.headers.get("ETag", "")
        response_validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified")
        if validator and response_validator and response_validator != validator:
          raise ValueError("The remote file changed during the pause")
        validator = validator or response_validator

        with open(temporary_path, "ab" if saved_bytes else "wb") as output:
          if output.tell() != saved_bytes:
            raise OSError("Partial fragment size changed on disk")
          for chunk in response.iter_content(chunk_size=self.chunk_size):
            if not chunk:
              continue
            output.write(chunk)
            downloaded_bytes += len(chunk)
            self.update_download_progress(index, a, downloaded_bytes, filesize, start)
            with self.schedule:
              if self.stopping or (
                filesize is not None and downloaded_bytes < b - a + 1 and self.is_file_paused(index)
              ):
                raise FragmentPaused

        if downloaded_bytes == 0 or (filesize is not None and downloaded_bytes != b - a + 1):
          raise requests.RequestException("Incomplete fragment")

        os.replace(temporary_path, fragment_path)

      with self.progress_lock:
        state = next(item for item in self.speed_file if item["id"] == index)
        state["partial"].pop(a, None)
        state["speed"] += downloaded_bytes
        state["active_fragments"].pop(a, None)

      return fragment_path

    except FragmentPaused:
      if downloaded_bytes:
        with self.progress_lock:
          state["partial"][a] = (downloaded_bytes, validator)
      raise

    except Exception:
      with self.progress_lock:
        state = next(item for item in self.speed_file if item["id"] == index)
        state["active_fragments"].pop(a, None)
        state["partial"].pop(a, None)
      if os.path.exists(temporary_path):
        os.remove(temporary_path)
      raise

  def prepare_download(self, filename, dest, url, filesize, index):
    if not filename.strip():
      filename = os.path.basename(urllib.parse.urlparse(url).path)

    if index in self.file_states:
      state, q, start, start_time = self.file_states[index]
      start += time.perf_counter() - state.pop("suspended_at")
    else:
      state, q, start, start_time = self.prepare_fragments(filename, dest, url, filesize, index)

    self.run_fragment_workers(q, url, dest, filesize, index, start, start_time)

    if state["failed"]:
      self.status_updated.emit(index, 3)
      return True
    if not q.empty():
      state["suspended_at"] = time.perf_counter()
      self.file_states[index] = (state, q, start, start_time)
      self.update_download_progress(index, None, 0, filesize, start)
      return False

    with self.schedule:
      if self.stopping:
        return True
      self.assembling.add(index)
      self.schedule.notify_all()
    self.create_file(dest, filename, index, filesize)
    return True

  def fragment_quota(self):
    transferring = sum(
      not self.is_file_paused(index)
      for index in self.active_files - self.assembling
    )
    available_slots = self.num_threads - len(self.active_files)
    waiting = sum(not self.is_file_paused(info[4]) for info in self.pending)
    sharing = transferring + min(available_slots, waiting)
    return max(1, self.total_threads // max(1, sharing))

  def run_fragment_workers(self, q, url, dest, filesize, index, start, start_time):
    def run_worker():
      try:
        with requests.Session() as session:
          self.download_worker(session, q, url, dest, filesize, index, start, start_time)
      finally:
        with self.schedule:
          self.fragment_workers[index] -= 1
          self.schedule.notify_all()

    with self.schedule:
      self.fragment_workers[index] = 0
      try:
        while True:
          running = self.fragment_workers[index]
          if not self.stopping and not self.is_file_paused(index):
            free = self.total_threads - sum(self.fragment_workers.values())
            count = min(q.qsize(), free, self.fragment_quota() - running)
            for _ in range(count):
              self.fragment_workers[index] += 1
              try:
                Thread(target=run_worker).start()
              except RuntimeError:
                self.fragment_workers[index] -= 1
                raise
          if not self.fragment_workers[index]:
            if self.stopping or self.is_file_paused(index) or q.empty():
              break
          self.schedule.wait()
      finally:
        self.schedule.wait_for(lambda: not self.fragment_workers[index])
        del self.fragment_workers[index]
        self.schedule.notify_all()

  def prepare_fragments(self, filename, dest, url, filesize, index):
    os.makedirs(dest, exist_ok=True)
    state = {
      "id": index,
      "url": url,
      "filename": filename,
      "speed": 0,
      "body": [],
      "failed": False,
      "active_fragments": {},
      "partial": {},
      "last_update": 0
    }

    with self.progress_lock:
      self.speed_file.append(state)

    q = Queue()

    if filesize is not None:
      if filesize <= 0:
        raise OSError("Invalid file size")

      for a in range(0, filesize, 3000000):
        b = min(a + 3000000, filesize) - 1
        q.put((a, b, filename))
    else:
      q.put((0, 0, filename))

    prepared = (state, q, time.perf_counter(), time.time())
    self.file_states[index] = prepared
    return prepared

  def next_download(self):
    with self.schedule:
      while self.unfinished and not self.stopping:
        for position, info in enumerate(self.pending):
          index = info[4]
          if not self.is_file_paused(index):
            self.active_files.add(index)
            self.schedule.notify_all()
            return self.pending.pop(position)
        self.schedule.wait()
      return None

  def download_thread(self):
    while True:
      info = self.next_download()
      if info is None:
        return
      name, folder, url, size, index = info
      done = True
      self.pause_changed.emit(index)
      try:
        done = self.prepare_download(
          urllib.parse.unquote(name), os.path.join(self.dest, folder), url, size, index
        )
      except Exception as error:
        self.download_error.emit(index, str(error))
        logging.exception("Download failed for %s", name)
        self.status_updated.emit(index, 3)
      finally:
        with self.schedule:
          self.active_files.discard(index)
          if done:
            self.unfinished.discard(index)
            self.paused_files.discard(index)
            self.resumed_files.discard(index)
            self.assembling.discard(index)
          else:
            self.pending.append(info)
          self.schedule.notify_all()
        self.pause_changed.emit(index)


ICON_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iad_icons")


def load_icon(name):
  svg_path = os.path.join(ICON_DIRECTORY, name + ".svg")
  if os.path.isfile(svg_path):
    return QIcon(svg_path)
  path = os.path.join(ICON_DIRECTORY, name + ".png")
  return QIcon(path) if os.path.isfile(path) else QIcon()


class Glyph(QLabel):
  def __init__(self, symbol, size=22, color="#60A5FA", parent=None):
    super().__init__(parent)
    icons = {"☷": "list", "▱": "folder", "◴": "gauge", "◷": "clock", "◉": "chart", "↓": "download"}
    self.setAlignment(Qt.AlignCenter)
    self.setFixedSize(size + 12, size + 12)
    icon = load_icon(icons.get(symbol, symbol))
    self.setPixmap(icon.pixmap(QSize(size, size)))
    self.setStyleSheet("background: transparent;")


def decorate_button(button, symbol):
  icons = {"Search": "search", "Browse": "folder-open", "Select all": "check-square",
    "Deselect all": "square", "Cancel": "x", "Add selected": "plus",
    "Sign in": "log-in", "Start downloads": "play", "Clear list": "broom",
    "Remove completed": "trash", "Exit": "log-out",
    "Pause all": "pause", "Resume all": "play"}
  name = icons.get(button.text())
  if name:
    button.setIcon(load_icon(name))
    button.setIconSize(QSize(20, 20))
  button.setCursor(Qt.PointingHandCursor)
  button.setMinimumHeight(43)


def file_icon(filename, status):
  suffix = os.path.splitext(filename)[1].lower()
  if suffix == ".zip":
    variant = {"Done": "gold", "Downloading": "rose"}.get(status, "violet")
    return load_icon(f"file-zip-{variant}")
  if suffix in (".jpg", ".jpeg"):
    return load_icon("file-jpg")
  return generic_file_icon(suffix)


def generic_file_icon(suffix):
  icon = QIcon()
  pixmap = QPixmap(32, 36)
  pixmap.fill(Qt.transparent)
  painter = QPainter(pixmap)
  painter.setRenderHint(QPainter.Antialiasing)
  colors = {".zip": "#F4B64F", ".7z": "#F4B64F", ".rar": "#F4B64F", ".iso": "#A78BFA", ".jpg": "#7269E9", ".jpeg": "#7269E9", ".png": "#60A5FA", ".pdf": "#F87171", ".xml": "#94A3B8", ".sqlite": "#94A3B8"}
  color = QColor(colors.get(suffix, "#8B9CB5"))
  painter.setPen(Qt.NoPen)
  painter.setBrush(color)
  painter.drawRoundedRect(QRectF(1, 2, 30, 32), 4, 4)
  painter.setBrush(QColor("#E8F2FF"))
  painter.drawRoundedRect(QRectF(1, 15, 30, 19), 3, 3)
  painter.setPen(QColor("#23344D"))
  font = QFont("Segoe UI", 6, QFont.Bold)
  painter.setFont(font)
  painter.drawText(QRect(1, 15, 30, 19), Qt.AlignCenter, (suffix[1:4] or "FILE").upper())
  painter.end()
  icon.addPixmap(pixmap)
  return icon


class ChecklistDialog(QDialog):
  def __init__(self, parent=None):
    super().__init__(parent)
    self.parent = parent
    self.name = self.parent.name
    self.list_files = self.parent.list_files
    self.setWindowTitle("IAD - Select files")
    self.setMinimumSize(690, 510)
    self.resize(800, 643)
    self.setObjectName("filePicker")
    self.setWindowIcon(load_icon("login-emblem"))
    assets = QDir.fromNativeSeparators(ICON_DIRECTORY)
    self.setStyleSheet("""
      QDialog#filePicker { border-image: url("ASSETS/login-background.svg"); }
      QLabel#pickerTitle { font-size: 32px; font-weight: bold; color: #f5faff; }
      QLabel#pickerSubtitle { font-size: 18px; color: #c9e0f6; }
      QLabel#pickerCount {
        border: 1px solid #358dd6; border-radius: 13px; background: #113259;
        color: #f0f8ff; font-size: 19px; padding: 0 15px;
      }
      QLineEdit#pickerSearch {
        background: #0c2a4c; border: 1px solid #4aa5ed; border-radius: 13px;
        min-height: 43px; padding: 0 12px; font-size: 17px; color: #edf7ff;
      }
      QLineEdit#pickerSearch:focus { border: 2px solid #57c8ff; }
      QListView#fileSelectionList {
        background: #09223e; alternate-background-color: #13365c;
        border: 1px solid #3384c8; border-radius: 13px; padding: 7px;
        font-size: 17px; color: #eff6ff; outline: none;
      }
      QListView#fileSelectionList::item { min-height: 37px; padding: 3px 14px; border-radius: 6px; }
      QListView#fileSelectionList::item:selected { background: #245781; }
      QListView#fileSelectionList::indicator { width: 20px; height: 20px; }
      QListView#fileSelectionList::indicator:unchecked { image: url("ASSETS/picker-empty.svg"); }
      QListView#fileSelectionList::indicator:checked { image: url("ASSETS/picker-check.svg"); }
      QScrollBar:vertical { background: #184672; width: 9px; border-radius: 4px; }
      QScrollBar::handle:vertical { background: #8ed2ff; min-height: 30px; margin: 0; border-radius: 4px; }
      QPushButton#pickerButton, QPushButton#pickerPrimary, QPushButton#pickerSelect {
        border-radius: 13px; border: 1px solid #358dd6;
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #184773,stop:1 #0a2546);
        color: #f4f9ff; font-size: 18px; font-weight: bold; padding: 0 15px;
      }
      QPushButton#pickerSelect { border-color: #25bfff; }
      QPushButton#pickerPrimary {
        border: 2px solid #6edcff; font-size: 20px;
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #19c1ff,stop:.45 #0985ef,stop:1 #0559dd);
      }
      QPushButton#pickerPrimary:disabled {
        color: #9bbad9; border-color: #477da9; background: #153d67;
      }
      QPushButton#pickerButton:hover, QPushButton#pickerSelect:hover, QPushButton#pickerPrimary:enabled:hover {
        background: #226198;
      }
      QPushButton#pickerButton:focus, QPushButton#pickerSelect:focus, QPushButton#pickerPrimary:focus {
        border-color: #98e8ff;
      }
    """.replace("ASSETS", assets))
    self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

    root = QVBoxLayout(self)
    root.setContentsMargins(25, 24, 24, 48)
    root.setSpacing(16)

    header = QHBoxLayout()
    heading = QVBoxLayout()
    heading.setSpacing(2)
    title = QLabel("Files found")
    title.setObjectName("pickerTitle")
    subtitle = QLabel("Choose which files or folders to download")
    subtitle.setObjectName("pickerSubtitle")
    heading.addWidget(title)
    heading.addWidget(subtitle)
    header.addWidget(Glyph("picker-list", 35), 0, Qt.AlignTop)
    header.addSpacing(14)
    header.addLayout(heading)
    header.addStretch()
    self.counter = QLabel("")
    self.counter.setObjectName("pickerCount")
    self.counter.setFixedHeight(53)
    counter_box = QHBoxLayout(self.counter)
    counter_box.setContentsMargins(9, 0, 0, 0)
    counter_box.addWidget(Glyph("picker-check", 23))
    counter_box.addStretch()
    self.counter.setIndent(49)
    header.addWidget(self.counter)
    root.addLayout(header)

    self.search_input = QLineEdit()
    self.search_input.setObjectName("pickerSearch")
    self.search_input.addAction(load_icon("search"), QLineEdit.LeadingPosition)
    palette = self.search_input.palette()
    palette.setColor(QPalette.PlaceholderText, QColor("#99c2e9"))
    self.search_input.setPalette(palette)
    self.search_input.setPlaceholderText("Search files or folders...")
    self.search_input.setClearButtonEnabled(True)
    self.search_input.setFixedHeight(45)
    self.search_input.textChanged.connect(self.filter_items)
    root.addWidget(self.search_input)

    self.model = QStandardItemModel(self)
    self.list_view = QListView()
    self.list_view.setModel(self.model)
    self.list_view.setObjectName("fileSelectionList")
    self.list_view.setAlternatingRowColors(True)
    self.list_view.setUniformItemSizes(True)
    self.list_view.setSpacing(2)
    self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    self.list_view.setIconSize(QSize(31, 30))
    root.addWidget(self.list_view, 1)

    controls = QHBoxLayout()
    controls.setSpacing(13)
    self.select_button = QPushButton("Select all")
    self.select_button.setObjectName("pickerSelect")
    decorate_button(self.select_button, "☑")
    self.select_button.setIcon(load_icon("picker-check"))
    self.select_button.clicked.connect(self.select_all_files)
    controls.addWidget(self.select_button)
    self.unselect_button = QPushButton("Deselect all")
    self.unselect_button.setObjectName("pickerButton")
    decorate_button(self.unselect_button, "☐")
    self.unselect_button.setIcon(load_icon("picker-empty"))
    self.unselect_button.clicked.connect(self.deselect_all_files)
    controls.addWidget(self.unselect_button)
    controls.addStretch()
    root.addLayout(controls)

    footer = QHBoxLayout()
    footer.setSpacing(13)
    footer.addStretch()
    cancel_button = QPushButton("Cancel")
    cancel_button.setObjectName("pickerButton")
    decorate_button(cancel_button, "×")
    cancel_button.clicked.connect(self.reject)
    footer.addWidget(cancel_button)
    self.ok_button = QPushButton("Add selected")
    self.ok_button.setObjectName("pickerPrimary")
    decorate_button(self.ok_button, "+")
    self.ok_button.clicked.connect(self.on_accepted)
    footer.addWidget(self.ok_button)
    root.addLayout(footer)

    for button in (self.select_button, self.unselect_button, cancel_button, self.ok_button):
      button.setAutoDefault(False)
      button.setIconSize(QSize(22, 22))
    self.search_input.setFocus()

    for file_info in self.list_files:
      filename = urllib.parse.unquote(file_info[1])
      item = QStandardItem(filename)
      item.setEditable(False)
      item.setCheckable(True)
      item.setCheckState(Qt.Unchecked)
      item.setToolTip(filename)
      if filename.endswith("/"):
        item.setIcon(load_icon("folder"))
      elif filename.lower().endswith(".png"):
        item.setIcon(load_icon("picker-png"))
      else:
        item.setIcon(file_icon(filename, "Queued"))
      self.model.appendRow(item)

    self.model.itemChanged.connect(self.update_counter)
    self.update_counter()

  def update_counter(self, item=None):
    count = sum(self.model.item(i).checkState() == Qt.Checked for i in range(self.model.rowCount()))
    self.counter.setText(f"{count} / {self.model.rowCount()} selected")
    self.ok_button.setEnabled(count > 0)

  def filter_items(self, text):
    term = text.casefold().strip()
    for i in range(self.model.rowCount()):
      self.list_view.setRowHidden(i, bool(term) and term not in self.model.item(i).text().casefold())

  def on_accepted(self):
    self.choices = [self.list_files[i] for i in range(self.model.rowCount())
                    if self.model.item(i).checkState() == Qt.Checked]
    self.accept()

  def set_all_checked(self, checked):
    previous_state = self.model.blockSignals(True)
    try:
      for row in range(self.model.rowCount()):
        item = self.model.item(row)
        if item is not None:
          item.setData(Qt.Checked if checked else Qt.Unchecked, Qt.CheckStateRole)
    finally:
      self.model.blockSignals(previous_state)
    self.list_view.viewport().update()
    self.update_counter()

  def select_all_files(self):
    self.set_all_checked(True)

  def deselect_all_files(self):
    self.set_all_checked(False)


class WLogin(QDialog):
  def __init__(self, parent=None):
    super().__init__(parent)
    self.setWindowTitle("IAD - Sign in")
    self.setObjectName("signIn")
    self.setFixedSize(580, 604)
    self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    self.setWindowIcon(load_icon("login-emblem"))
    background = QDir.fromNativeSeparators(ICON_DIRECTORY) + "/login-background.svg"
    self.setStyleSheet("""
      QDialog#signIn { border-image: url("BACKGROUND"); }
      QLabel#loginTitle { color: #f8fbff; font-size: 39px; font-weight: 750; }
      QLabel#loginSubtitle { color: #d5e6fa; font-size: 17px; }
      QLabel#loginFooter { color: #bfd9f5; font-size: 19px; }
      QLabel#loginFieldLabel { color: #f2f7ff; font-size: 16px; font-weight: bold; }
      QLabel#loginMessage { font-size: 13px; }
      QFrame[loginField="true"] {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #192e49,stop:.55 #193554,stop:1 #142942);
        border: 2px solid #3c87c7; border-radius: 12px;
      }
      QFrame[loginField="true"][active="true"] { border: 2px solid #34d5fa; border-bottom-color: #34a6ff; }
      QLineEdit[loginInput="true"] {
        background: transparent; border: none; padding: 0; min-height: 0;
        font-size: 17px; color: #f0f7ff;
      }
      QPushButton#showPassword {
        background: transparent; border: none; padding: 0 5px;
        color: #f3f8ff; font-size: 16px; font-weight: bold;
      }
      QPushButton#showPassword:hover, QPushButton#showPassword:focus { background: #28486d; }
      QPushButton#loginSubmit {
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #28aafb,stop:.4 #087cf5,stop:1 #064de4);
        border: 2px solid #2caef9; border-top-color: #70e3ff; border-radius: 17px;
        color: white; font-size: 25px; font-weight: bold; padding: 0;
      }
      QPushButton#loginSubmit:hover { background: #2189f7; }
      QPushButton#loginSubmit:pressed { background: #0956c3; }
      QPushButton#loginSubmit:disabled { background: #234569; color: #a8c6e6; border-color: #3a658e; }
    """.replace("BACKGROUND", background))

    root = QVBoxLayout(self)
    root.setContentsMargins(51, 34, 51, 0)
    root.setSpacing(0)
    root.addWidget(self.icon_label("login-emblem", 84), alignment=Qt.AlignHCenter)

    title = QLabel("Welcome to IAD")
    title.setObjectName("loginTitle")
    title.setAlignment(Qt.AlignCenter)
    title.setFixedHeight(50)
    root.addWidget(title)
    subtitle_row = QHBoxLayout()
    subtitle_row.setSpacing(15)
    subtitle = QLabel("Sign in with your Internet Archive account")
    subtitle.setObjectName("loginSubtitle")
    subtitle.setAlignment(Qt.AlignCenter)
    for reverse in (False, True):
      rule = QFrame()
      rule.setFixedHeight(2)
      start, end = ("#27c9f7", "transparent") if reverse else ("transparent", "#27c9f7")
      rule.setStyleSheet(f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {start},stop:1 {end});")
      if reverse:
        subtitle_row.addWidget(subtitle)
      subtitle_row.addWidget(rule, 1)
    root.addLayout(subtitle_row)
    root.addSpacing(24)

    self.user = QLineEdit()
    self.user.setPlaceholderText("Your username or email")
    self.user.setAccessibleName("Username")
    self.user.setClearButtonEnabled(True)
    self.add_field(root, "USERNAME", "login-user", self.user)
    root.addSpacing(14)

    self.password_input = QLineEdit()
    self.password_input.setPlaceholderText("Your password")
    self.password_input.setAccessibleName("Password")
    self.password_input.setEchoMode(QLineEdit.Password)
    password_row = self.add_field(root, "PASSWORD", "login-lock", self.password_input)
    divider = QFrame()
    divider.setFixedSize(1, 25)
    divider.setStyleSheet("background: #4675a5;")
    password_row.addWidget(divider)
    self.toggle_password = QPushButton(" Show")
    self.toggle_password.setAccessibleName("Show password")
    self.toggle_password.setObjectName("showPassword")
    self.toggle_password.setFixedSize(82, 35)
    self.toggle_password.setAutoDefault(False)
    self.toggle_password.setCursor(Qt.PointingHandCursor)
    self.toggle_password.setIcon(load_icon("login-eye"))
    self.toggle_password.setIconSize(QSize(21, 21))
    self.toggle_password.clicked.connect(self.toggle_password_visibility)
    password_row.addWidget(self.toggle_password)

    self.message = QLabel("")
    self.message.setObjectName("loginMessage")
    self.message.setWordWrap(True)
    self.message.setFixedHeight(31)
    root.addWidget(self.message)

    self.button_login = QPushButton("  Sign in")
    self.button_login.setAccessibleName("Sign in")
    self.button_login.setObjectName("loginSubmit")
    self.button_login.setFixedHeight(57)
    self.button_login.setFocusPolicy(Qt.TabFocus)
    self.button_login.setDefault(True)
    self.button_login.setIcon(load_icon("login-enter"))
    self.button_login.setIconSize(QSize(31, 31))
    self.button_login.setCursor(Qt.PointingHandCursor)
    self.user.returnPressed.connect(self.login)
    self.password_input.returnPressed.connect(self.login)
    self.button_login.clicked.connect(self.login)
    root.addWidget(self.button_login)
    root.addSpacing(16)
    footer = QLabel("Internet Archive Downloader")
    footer.setObjectName("loginFooter")
    footer.setAlignment(Qt.AlignCenter)
    root.addWidget(footer)
    root.addStretch()
    self.user.setFocus()

  def icon_label(self, name, size):
    label = QLabel()
    label.setPixmap(load_icon(name).pixmap(size, size))
    label.setFixedSize(size, size)
    label.setAlignment(Qt.AlignCenter)
    return label

  def add_field(self, root, title, icon, field):
    label = QLabel(title)
    label.setObjectName("loginFieldLabel")
    label.setFixedHeight(21)
    label.setContentsMargins(2, 0, 0, 0)
    label.setBuddy(field)
    root.addWidget(label)
    root.addSpacing(7)

    frame = QFrame()
    frame.setProperty("loginField", True)
    frame.setFixedHeight(51)
    row = QHBoxLayout(frame)
    row.setContentsMargins(21, 0, 5, 0)
    row.setSpacing(15)
    row.addWidget(self.icon_label(icon, 24))
    field.setProperty("loginInput", True)
    palette = field.palette()
    palette.setColor(QPalette.PlaceholderText, QColor("#b7c9df"))
    field.setPalette(palette)
    field.installEventFilter(self)
    row.addWidget(field, 1)
    root.addWidget(frame)
    return row

  def eventFilter(self, watched, event):
    if isinstance(watched, QLineEdit) and event.type() in (QEvent.FocusIn, QEvent.FocusOut):
      frame = watched.parentWidget()
      frame.setProperty("active", event.type() == QEvent.FocusIn)
      frame.style().unpolish(frame)
      frame.style().polish(frame)
      frame.update()
    return super().eventFilter(watched, event)

  def reject(self):
    if hasattr(self, "_login_thread") and self._login_thread.isRunning():
      return
    super().reject()

  def closeEvent(self, event):
    if hasattr(self, "_login_thread") and self._login_thread.isRunning():
      event.ignore()
      return
    super().closeEvent(event)

  def toggle_password_visibility(self):
    visible = self.password_input.echoMode() == QLineEdit.Normal
    self.password_input.setEchoMode(QLineEdit.Password if visible else QLineEdit.Normal)
    self.toggle_password.setText(" Show" if visible else " Hide")
    self.toggle_password.setAccessibleName("Show password" if visible else "Hide password")
    self.toggle_password.setIcon(load_icon("login-eye" if visible else "login-eye-off"))

  def login(self):
    if not self.user.text().strip():
      self.message.setText("Enter your username or email.")
      self.user.setFocus()
      return
    if not self.password_input.text():
      self.message.setText("Enter your password.")
      self.password_input.setFocus()
      return
    if hasattr(self, "_login_thread") and self._login_thread.isRunning():
      return
    self.button_login.setEnabled(False)
    self.message.setText("Verifying credentials...")
    self._archive_org = ArchiveOrg(self.user.text(), self.password_input.text(), None, None, None)
    self._login_thread = QThread(self)
    self._archive_org.moveToThread(self._login_thread)
    self._archive_org._response.connect(self.login_result)
    self._archive_org._response.connect(self._login_thread.quit)
    self._login_thread.started.connect(self._archive_org.login)
    self._login_thread.finished.connect(self._archive_org.deleteLater)
    self._login_thread.start()

  @pyqtSlot(object)
  def login_result(self, response):
    try:
      message = response.json()
      if message.get("success") is True and response.ok:
        self.cookies = response.cookies
        self.message.setText("Signed in successfully")
        self._main = Main(self)
        self._main.show()
        self.hide()
      else:
        self.message.setText("Invalid credentials. Please try again.")
    except (ValueError, AttributeError, TypeError):
      self.message.setText("Could not verify the response. Please try again.")
    self.button_login.setEnabled(True)


class FilenameDelegate(QStyledItemDelegate):
  def paint(self, painter, option, index):
    filename_option = QStyleOptionViewItem(option)
    self.initStyleOption(filename_option, index)
    icon = QIcon(filename_option.icon)
    filename_option.features &= ~QStyleOptionViewItem.HasDecoration
    filename_option.icon = QIcon()
    style = option.widget.style()
    background = QStyleOptionViewItem(filename_option)
    background.text = ""
    style.drawControl(QStyle.CE_ItemViewItem, background, painter, option.widget)
    filename_option.rect.adjust(61, 0, 0, 0)
    style.drawControl(QStyle.CE_ItemViewItem, filename_option, painter, option.widget)
    icon.paint(painter, QRect(option.rect.x() + 9, option.rect.center().y() - 13, 29, 27))


class ProgressBar(QProgressBar):
  def __init__(self, value, parent=None):
    super().__init__(parent)
    self.setMinimum(0)
    self.setMaximum(100)
    self.setValue(value)
    self.setFixedHeight(24)

class Main(QDialog):
  def __init__(self, parent=None):
    super(Main, self).__init__()
    self.cookies = parent.cookies
    self.info_files = []
    self.count = 0
    self.finished = False
    self.download_stats = {}
    self.download_sizes = {}
    self.download_size_bytes = {}
    self.destination_path = ""
    self.setWindowTitle("IAD - Internet Archive Downloader")
    self.setMinimumSize(1050, 720)
    self.setWindowFlags(Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
    available = QApplication.primaryScreen().availableGeometry()
    self.resize(min(1586, available.width()), min(944, available.height() - 48))

    root = QVBoxLayout(self)
    root.setContentsMargins(28, 17, 27, 29)
    root.setSpacing(0)

    header = QHBoxLayout()
    header.setContentsMargins(6, 0, 4, 0)
    header.setSpacing(24)
    logo = QLabel()
    logo.setPixmap(load_icon("arrow").pixmap(32, 32))
    logo.setObjectName("iconLogo")
    logo.setAlignment(Qt.AlignCenter)
    logo.setFixedSize(60, 57)
    header.addWidget(logo)
    heading = QVBoxLayout()
    heading.setSpacing(0)
    title = QLabel("IAD")
    title.setFixedHeight(34)
    title.setObjectName("appTitle")
    subtitle = QLabel("Internet Archive Downloader")
    subtitle.setFixedHeight(25)
    subtitle.setObjectName("subtitle")
    heading.addWidget(title)
    heading.addWidget(subtitle)
    header.addLayout(heading)
    header.addStretch()
    self.activity_label = QLabel()
    self.activity_label.setObjectName("statusPill")
    self.activity_label.setFixedHeight(47)
    self.set_activity("Ready")
    header.addWidget(self.activity_label)
    root.addLayout(header)
    root.addSpacing(16)

    url_label = QLabel("ARCHIVE URL")
    url_label.setObjectName("fieldLabel")
    root.addWidget(url_label)
    root.addSpacing(3)
    url_row = QHBoxLayout()
    url_row.setContentsMargins(4, 0, 3, 0)
    url_row.setSpacing(16)
    self.url_line = QLineEdit()
    self.url_line.setObjectName("archiveUrl")
    self.url_line.setPlaceholderText("https://archive.org/download/collection-name")
    clear = self.url_line.addAction(load_icon("x"), QLineEdit.TrailingPosition)
    clear.setToolTip("Clear URL")
    clear.triggered.connect(self.url_line.clear)
    self.url_line.setFixedHeight(48)
    self.url_line.returnPressed.connect(self.search)
    url_row.addWidget(self.url_line, 1)
    self.button_search = QPushButton("Search")
    self.button_search.setObjectName("primaryButton")
    self.button_search.setFixedSize(164, 50)
    decorate_button(self.button_search, "search")
    self.button_search.setIconSize(QSize(25, 25))
    self.button_search.clicked.connect(self.search)
    url_row.addWidget(self.button_search)
    root.addLayout(url_row)
    root.addSpacing(22)

    settings_row = QHBoxLayout()
    settings_row.setContentsMargins(4, 0, 3, 0)
    settings_row.setSpacing(24)
    destination_layout = QVBoxLayout()
    destination_layout.setSpacing(4)
    destination_title = QLabel("SAVE TO")
    destination_title.setObjectName("fieldLabel")
    destination_layout.addWidget(destination_title)
    destination_panel = QFrame()
    destination_panel.setObjectName("destinationPanel")
    destination_panel.setFixedHeight(47)
    destination_line = QHBoxLayout(destination_panel)
    destination_line.setContentsMargins(13, 0, 0, 0)
    destination_line.setSpacing(6)
    destination_line.addWidget(Glyph("folder", 25))
    self.folder_path_label = QLabel("Choose a destination folder")
    self.folder_path_label.setObjectName("subtitle")
    self.folder_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    destination_line.addWidget(self.folder_path_label, 1)
    browse_button = QPushButton("Browse")
    browse_button.setObjectName("secondaryButton")
    decorate_button(browse_button, "folder-open")
    browse_button.setFixedSize(149, 46)
    browse_button.setIconSize(QSize(25, 25))
    browse_button.clicked.connect(self.select_folder)
    destination_line.addWidget(browse_button)
    destination_layout.addWidget(destination_panel)
    settings_row.addLayout(destination_layout, 1)
    divider = QFrame()
    divider.setObjectName("divider")
    divider.setFixedSize(1, 48)
    settings_row.addWidget(divider, alignment=Qt.AlignBottom)
    concurrency_layout = QVBoxLayout()
    concurrency_layout.setSpacing(4)
    concurrency_title = self.concurrency_title = QLabel("CONCURRENT FILES")
    concurrency_title.setObjectName("fieldLabel")
    concurrency_layout.addWidget(concurrency_title)
    concurrency_panel = self.concurrency_panel = QFrame()
    concurrency_panel.setObjectName("destinationPanel")
    concurrency_panel.setFixedSize(204, 47)
    self.concurrency_opacity = QGraphicsOpacityEffect(concurrency_panel)
    self.concurrency_opacity.setOpacity(0.52)
    self.concurrency_opacity.setEnabled(False)
    concurrency_panel.setGraphicsEffect(self.concurrency_opacity)
    concurrency_line = QHBoxLayout(concurrency_panel)
    concurrency_line.setContentsMargins(12, 0, 6, 0)
    concurrency_line.setSpacing(8)
    concurrency_line.addWidget(Glyph("layers", 25))
    self.concurrent_files = QSpinBox()
    self.concurrent_files.setRange(1, MAX_THREADS)
    icons = QDir.fromNativeSeparators(ICON_DIRECTORY)
    self.concurrent_files.setStyleSheet(
      f'QSpinBox::up-arrow {{ image: url("{icons}/up.svg"); }}'
      f'QSpinBox::down-arrow {{ image: url("{icons}/down.svg"); }}'
    )
    self.concurrent_files.setToolTip("Number of simultaneous files. Fragment requests share the existing worker limit.")
    concurrency_line.addWidget(self.concurrent_files, 1)
    concurrency_layout.addWidget(concurrency_panel)
    settings_row.addLayout(concurrency_layout)
    root.addLayout(settings_row)
    root.addSpacing(23)

    metrics_row = QHBoxLayout()
    metrics_row.setContentsMargins(3, 0, 1, 0)
    metrics_row.setSpacing(25)
    self.speed_value, speed_card = self.make_metric("AVG SPEED", "0 B/s", "gauge")
    self.downloaded_value, downloaded_card = self.make_metric("DOWNLOADED", "0 B", "download")
    self.remaining_value, remaining_card = self.make_metric("REMAINING", "--:--:--", "clock")
    self.overall_value, overall_card = self.make_metric("OVERALL PROGRESS", "0%", "chart")
    for card in (speed_card, downloaded_card, remaining_card, overall_card):
      metrics_row.addWidget(card, 1)
    root.addLayout(metrics_row)
    root.addSpacing(17)

    overall_panel = QFrame()
    overall_panel.setObjectName("panel")
    overall_panel.setContentsMargins(0, 0, 0, 0)
    overall_layout = QVBoxLayout(overall_panel)
    overall_panel.setFixedHeight(103)
    overall_layout.setContentsMargins(20, 6, 20, 6)
    overall_layout.setSpacing(5)
    overall_top = QHBoxLayout()
    overall_title = QLabel("Overall progress")
    overall_title.setObjectName("sectionTitle")
    overall_top.addWidget(overall_title)
    overall_top.addStretch()
    self.overall_percent = QLabel("0%")
    self.overall_percent.setObjectName("overallPercent")
    overall_top.addWidget(self.overall_percent)
    overall_layout.addLayout(overall_top)
    self.overall_bar = QProgressBar()
    self.overall_bar.setRange(0, 100)
    self.overall_bar.setValue(0)
    self.overall_bar.setTextVisible(False)
    self.overall_bar.setObjectName("overallBar")
    overall_layout.addWidget(self.overall_bar)
    overall_bottom = QHBoxLayout()
    self.overall_count = QLabel("0 files completed")
    self.overall_count.setObjectName("muted")
    overall_bottom.addWidget(self.overall_count)
    overall_bottom.addStretch()
    self.overall_bytes = QLabel("0 B / 0 B")
    self.overall_bytes.setObjectName("muted")
    overall_bottom.addWidget(self.overall_bytes)
    overall_layout.addLayout(overall_bottom)
    overall_row = QHBoxLayout()
    overall_row.setContentsMargins(3, 0, 2, 0)
    overall_row.addWidget(overall_panel)
    root.addLayout(overall_row)
    root.addSpacing(18)

    downloads_header = QHBoxLayout()
    downloads_title = QLabel("Downloads")
    downloads_title.setObjectName("sectionTitle")
    downloads_header.addSpacing(5)
    downloads_header.addWidget(downloads_title)
    downloads_header.addStretch()
    downloads_header.setSpacing(18)
    self.button_pause = QPushButton("Pause all")
    self.button_pause.setFixedWidth(134)
    self.button_resume = QPushButton("Resume all")
    self.button_resume.setFixedWidth(147)
    for button, paused in ((self.button_pause, True), (self.button_resume, False)):
      button.setObjectName("queueButton")
      decorate_button(button, "")
      button.setEnabled(False)
      button.clicked.connect(lambda checked, pause=paused: self.pause_downloads(pause))
      downloads_header.addWidget(button)
    self.button_remove_completed = QPushButton("Remove completed")
    self.button_remove_completed.setObjectName("queueButton")
    self.button_remove_completed.setFixedWidth(210)
    decorate_button(self.button_remove_completed, "⌫")
    self.button_remove_completed.clicked.connect(self.remove_completed)
    downloads_header.addWidget(self.button_remove_completed)
    downloads_header.addSpacing(28)
    self.file_count_label = QLabel("0 files")
    self.file_count_label.setObjectName("muted")
    self.file_count_label.setContentsMargins(0, 0, 6, 0)
    downloads_header.addWidget(self.file_count_label)
    root.addLayout(downloads_header)
    root.addSpacing(10)

    self.closing_for_shutdown = False
    self.search_animation = QTimer(self)
    self.search_animation.setInterval(70)
    self.search_animation.timeout.connect(self.draw_search_spinner)
    self.spinner_angle = 0
    self.create_table()
    root.addWidget(self.table_widget, 1)
    root.addSpacing(30)

    footer = QHBoxLayout()
    footer.setSpacing(14)
    self.button_start = QPushButton("Start downloads")
    self.button_start.setObjectName("primaryButton")
    decorate_button(self.button_start, "▷")
    self.button_start.setEnabled(False)
    self.button_start.clicked.connect(self.select_start)
    self.button_start.setFixedSize(223, 54)
    footer.addWidget(self.button_start)
    self.button_clean = QPushButton("Clear list")
    self.button_clean.setObjectName("secondaryButton")
    decorate_button(self.button_clean, "broom")
    self.button_clean.setIconSize(QSize(26, 26))
    self.button_clean.clicked.connect(self.select_clean)
    self.button_clean.setFixedSize(154, 54)
    footer.addWidget(self.button_clean)
    footer.addStretch()
    button_exit = QPushButton("Exit")
    button_exit.setObjectName("secondaryButton")
    decorate_button(button_exit, "↪")
    button_exit.clicked.connect(self.close)
    button_exit.setFixedSize(120, 54)
    footer.addWidget(button_exit)
    root.addLayout(footer)
    self.url_line.setFocus(Qt.OtherFocusReason)
    for button in self.findChildren(QPushButton):
      button.setAutoDefault(False)

  def set_activity(self, text):
    color = {"Ready": "#1ab76a", "Paused": "#f4b64f", "Pausing…": "#f4b64f"}.get(text, "#328bff")
    self.activity_label.setText(f'<span style="color:{color}; font-size:26px">●</span>&nbsp; {text}')

  def pause_downloads(self, paused):
    if self._downloader.isRunning():
      self._downloader.set_paused(paused)
      self.update_dashboard()

  def keyPressEvent(self, event):
    if event.key() == Qt.Key_Escape:
      self.close()
      return
    super().keyPressEvent(event)

  def closeEvent(self, event):
    downloader = getattr(self, "_downloader", None)
    if downloader and downloader.isRunning():
      self.closing_for_shutdown = True
      downloader.request_stop()
      self.hide()
      QTimer.singleShot(0, self.finish_shutdown)
      event.ignore()
      return
    if any(thread.isRunning() for thread in self.findChildren(QThread)):
      self.closing_for_shutdown = True
      self._archive_org.search_cancelled.set()
      self.search_animation.stop()
      self.hide()
      event.ignore()
      return
    super().closeEvent(event)

  @pyqtSlot()
  def finish_shutdown(self):
    if self.closing_for_shutdown and not any(thread.isRunning() for thread in self.findChildren(QThread)):
      self.closing_for_shutdown = False
      self.close()
      QApplication.quit()

  def get_theme(self):
    return """
      QDialog { background-color: #0C1728; color: #EAF2FF; font-family: 'Segoe UI'; font-size: 13px; }
      QLabel { color: #EAF2FF; background: transparent; }
      QLabel#iconLogo { background: #1E3A60; color: #60A5FA; border-radius: 15px; font-size: 34px; font-weight: bold; }
      QLabel#appTitle { font-size: 26px; font-weight: bold; }
      QLabel#muted { color: #B4C4DA; }
      QLabel#fieldLabel { color: #A8BEDA; font-size: 11px; font-weight: bold; }
      QLabel#sectionTitle { font-size: 18px; font-weight: bold; }
      QLabel#overallPercent { color: #DCEBFF; font-size: 18px; font-weight: bold; }
      QLabel#metricValue { color: #FFFFFF; font-size: 24px; font-weight: bold; }
      QLabel#statusPill { color: #D9E9FF; background: #1C304B; border-radius: 12px; padding: 11px 17px; font-weight: bold; }
      QFrame#panel, QFrame#metricPanel { background-color: #192B43; border: 1px solid #304966; border-radius: 13px; }
      QFrame#flatPanel { background: transparent; border: none; }
      QLineEdit { background: #1C2D45; color: #F8FAFC; border: 1px solid #46617E; border-radius: 9px; padding: 0px 14px; min-height: 44px; font-size: 14px; selection-background-color: #2563EB; }
      QComboBox { background: #1C2D45; color: #F8FAFC; border: 1px solid #46617E; border-radius: 9px; padding: 9px 13px; min-width: 135px; selection-background-color: #2563EB; }
      QLineEdit:focus, QComboBox:focus { border: 1px solid #60A5FA; }
      QComboBox QAbstractItemView { background: #1E3049; color: #F8FAFC; selection-background-color: #2563EB; }
      QPushButton { border-radius: 9px; padding: 10px 16px; font-size: 13px; font-weight: bold; }
      QPushButton#primaryButton { background: #2469F3; color: white; border: 1px solid #3882FF; }
      QPushButton#primaryButton:hover { background: #3684FF; }
      QPushButton#secondaryButton { background: #293F5B; color: #EDF5FF; border: 1px solid #334B68; }
      QPushButton#secondaryButton:hover { background: #385475; }
      QPushButton:disabled { background: #23334A; color: #7389A4; border-color: #23334A; }
      QTableWidget { background: #142238; alternate-background-color: #1C2D46; color: #E8F1FF; border: 1px solid #354E6B; border-radius: 12px; gridline-color: transparent; selection-background-color: #284568; font-size: 13px; }
      QHeaderView::section { background: #263C59; color: #E3ECFA; border: none; border-bottom: 1px solid #38516E; padding: 8px 4px; font-size: 12px; font-weight: bold; }
      QTableWidget::item { padding-left: 7px; }
      QProgressBar { background: #344A65; border: none; border-radius: 7px; color: white; text-align: center; min-height: 20px; font-weight: bold; }
      QProgressBar::chunk { background: #3288FF; border-radius: 7px; }
      QProgressBar#overallBar { min-height: 14px; max-height: 14px; border-radius: 7px; }
      QProgressBar#completedBar::chunk { background: #49B86A; }
      QProgressBar#failedBar::chunk { background: #F87171; }
      QScrollBar:vertical { background: #142238; width: 12px; margin: 0px; border: none; }
      QScrollBar::handle:vertical { background: #506A89; border-radius: 5px; min-height: 32px; margin: 2px; }
      QScrollBar::handle:vertical:hover { background: #7895B6; }
      QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; border: none; background: transparent; }
      QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
      QScrollBar:horizontal { background: #142238; height: 12px; margin: 0px; border: none; }
      QScrollBar::handle:horizontal { background: #506A89; border-radius: 5px; min-width: 32px; margin: 2px; }
      QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; border: none; background: transparent; }
      QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
      QListView#fileSelectionList { background: #142238; alternate-background-color: #1C2D46; color: #E8F1FF; border: 1px solid #354E6B; border-radius: 12px; padding: 10px; outline: none; font-size: 14px; }
      QListView#fileSelectionList::item { min-height: 36px; padding: 5px 10px; border-radius: 6px; }
      QListView#fileSelectionList::item:selected { background: #2C4D73; }
      QLabel#loginMessage { color: #FCA5A5; }
      QCheckBox { color: #E2E8F0; }
      QMessageBox { background: #101a29; }
      QDialog { background: #0e1827; font-size: 17px; }
      QLabel, QLineEdit, QSpinBox, QPushButton, QTableWidget, QHeaderView, QProgressBar {
        font-family: "Segoe UI";
      }
      QLabel#appTitle { font-size: 29px; color: #f6f8fc; }
      QLabel#subtitle { font-size: 20px; color: #d4e3f6; }
      QLabel#muted { color: #cfddf0; font-size: 18px; }
      QLabel#fieldLabel { color: #c7d9ed; font-size: 15px; padding-left: 7px; }
      QLabel#fieldLabel:disabled { color: #879bb4; }
      QLabel#sectionTitle, QLabel#overallPercent { color: #f8faff; font-size: 22px; }
      QLabel#metricValue { font-size: 30px; }
      QLabel#iconLogo { background: #1c3455; border-radius: 17px; }
      QLabel#statusPill { background: #1b2a3d; padding: 0 15px; font-size: 20px; font-weight: normal; }
      QFrame#panel, QFrame#metricPanel, QFrame#destinationPanel {
        background: #182638; border: 1px solid #344b64; border-radius: 14px;
      }
      QFrame#metricPanel { border-color: #293e55; }
      QFrame#metricPanel QLabel#fieldLabel { padding: 0; }
      QFrame#divider { background: #344b64; }
      QLineEdit#archiveUrl { background: #182638; border: 1px solid #405d79;
        border-radius: 12px; font-size: 21px; padding: 0 21px; }
      QLineEdit#archiveUrl:focus { border-color: #587c9f; }
      QSpinBox { background: transparent; border: none; font-size: 21px; color: #f5f8ff; padding: 3px 28px 3px 8px; }
      QSpinBox::up-button, QSpinBox::down-button { width: 24px; border: none; background: transparent; }
      QSpinBox::up-arrow, QSpinBox::down-arrow { width: 16px; height: 12px; }
      QPushButton#primaryButton, QPushButton#secondaryButton, QPushButton#queueButton {
        border-radius: 12px; font-size: 20px; padding: 0 15px;
      }
      QPushButton#primaryButton { background: #1c65ed; border-color: #2475fb; }
      QPushButton#secondaryButton, QPushButton#queueButton { background: #2c3e54; color: #f6f9ff; border: 1px solid #2c3e54; }
      QPushButton#queueButton { font-size: 16px; font-weight: 600; padding: 0 18px; }
      QPushButton#secondaryButton:hover, QPushButton#queueButton:hover { background: #395572; }
      QPushButton#primaryButton:disabled, QPushButton#secondaryButton:disabled, QPushButton#queueButton:disabled {
        background: #202e41; border-color: #202e41; color: #a6b6ca;
      }
      QPushButton#rowButton { background: transparent; border: none; padding: 0; }
      QPushButton#rowButton:hover { background: #304861; }
      QPushButton#rowButton::menu-indicator { image: none; width: 0; }
      QMenu { background: #20324a; color: #eaf2ff; }
      QMenu::item:selected { background: #395572; }
      QTableWidget::item { padding-left: 8px; }
      QTableWidget { background: #131f30; alternate-background-color: #192638;
        border: 1px solid #344d67; border-radius: 15px; font-size: 17px; }
      QHeaderView { background: transparent; }
      QHeaderView::section { background: #203148; font-size: 17px; padding: 0 8px; }
      QHeaderView::section:first, QHeaderView::section:only-one { border-top-left-radius: 14px; }
      QHeaderView::section:last { border-top-right-radius: 14px; }
      QProgressBar { background: #35485d; font-size: 17px; }
      QProgressBar::chunk { background: #2e89fa; }
      QProgressBar#completedBar::chunk { background: #56ad56; }
      QProgressBar#overallBar { min-height: 22px; max-height: 22px; border-radius: 11px; }
      QProgressBar#overallBar::chunk { border-radius: 11px;
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #237af0,stop:1 #3c9df8); }

    """

  def make_metric(self, label, value, symbol):
    card = QFrame()
    card.setObjectName("metricPanel")
    card.setFixedHeight(86)
    layout = QHBoxLayout(card)
    layout.setContentsMargins(14, 8, 14, 8)
    layout.setSpacing(21)
    layout.addWidget(Glyph(symbol, 46))
    values = QVBoxLayout()
    values.setSpacing(0)
    values.setAlignment(Qt.AlignVCenter)
    heading = QLabel(label)
    heading.setFixedHeight(21)
    heading.setObjectName("fieldLabel")
    amount = QLabel(value)
    amount.setFixedHeight(38)
    amount.setObjectName("metricValue")
    values.addWidget(heading)
    values.addWidget(amount)
    layout.addLayout(values, 1)
    return amount, card

  def remove_completed(self):
    if hasattr(self, "_downloader") and self._downloader.isRunning():
      return
    remaining = []
    for index, info in enumerate(self.info_files):
      status = self.table_widget.item(index, 2)
      if not status or status.text() != "Completed":
        remaining.append((*info[:4], len(remaining)))
    if len(remaining) == len(self.info_files):
      return
    self.info_files = remaining
    self.download_sizes.clear()
    self.download_size_bytes.clear()
    self.download_stats.clear()
    self.count = 0
    self.table_widget.setRowCount(0)
    for index, info in enumerate(remaining):
      size = info[3]
      self.download_sizes[index] = self.get_size_format(size) if size is not None else "--"
      self.download_size_bytes[index] = size or 0
      self.add_row_table(info[0], self.download_sizes[index], index)
    self.button_start.setEnabled(bool(remaining))
    self.finished = False
    self.update_dashboard()

  def update_dashboard(self):
    transferring = [item for item in self.download_stats.values()
                    if item["status"] in ("Downloading", "Pausing…")]
    speed = sum(item["speed"] for item in transferring)
    remaining_values = [item["remaining"] for item in transferring if item["remaining"] is not None]
    downloader = getattr(self, "_downloader", None)
    if downloader and downloader.isRunning() and not self.closing_for_shutdown:
      paused, runnable, draining = downloader.pause_summary()
      self.button_pause.setEnabled(runnable)
      self.button_resume.setEnabled(paused)
      if paused:
        activity = "Partially paused" if runnable else "Pausing…" if draining else "Paused"
      else:
        activity = "Downloading"
      self.set_activity(activity)
    self.speed_value.setText(self.get_size_format(speed) + "/s")
    self.remaining_value.setText(self.format_time(max(remaining_values)) if remaining_values else "--:--:--")
    self.file_count_label.setText(f"{len(self.info_files)} files · {self.get_size_format(sum(self.download_size_bytes.values()))}")
    total = sum(self.download_size_bytes.values())
    completed = sum(self.download_size_bytes.get(i, 0) for i in range(len(self.info_files))
                    if self.table_widget.item(i, 2) and self.table_widget.item(i, 2).text() == "Completed")
    partial = sum(min(self.download_size_bytes.get(i, 0), stat["bytes"])
                  for i, stat in self.download_stats.items()
                  if i < len(self.info_files) and self.table_widget.item(i, 2)
                  and self.table_widget.item(i, 2).text() != "Completed")
    current = min(total, completed + partial)
    self.downloaded_value.setText(self.get_size_format(current))
    percent = int(current * 100 / total) if total else 0
    self.overall_bar.setValue(percent)
    self.overall_percent.setText(f"{percent}%")
    self.overall_value.setText(f"{percent}%")
    self.overall_count.setText(f"{self.count} of {len(self.info_files)} files completed")
    self.overall_bytes.setText(f"{self.get_size_format(current)} / {self.get_size_format(total)}")

  def mousePressEvent(self, e):
    self.table_widget.clearSelection()

  def format_time(self, seconds):
    hours, rest = divmod(int(seconds), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"

  def get_size_format(self, b, factor=1024, suffix="B"):
    if b == 0:
      return "0 B"
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

  def message_search(self, title, text, *, heading=None):
    msg = QDialog(self)
    msg.setObjectName("iadNotice")
    msg.setWindowTitle(f"IAD - {title}")
    msg.setWindowIcon(load_icon("login-emblem"))
    msg.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    background = QDir.fromNativeSeparators(ICON_DIRECTORY) + "/notice-background.svg"
    msg.setStyleSheet("""
      QDialog#iadNotice { border-image: url("BACKGROUND"); }
      QLabel#noticeHeading { color: #f7faff; font-size: 25px; font-weight: bold; }
      QLabel#noticeText { color: #d8e9fb; font-size: 18px; }
      QFrame#noticeDivider { background: #31a7ee; }
      QPushButton#noticeOk {
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #0eb8f8,stop:.6 #0765e9,stop:1 #064ee1);
        border: 2px solid #27d3fa; border-radius: 11px; padding: 0;
        color: white; font-size: 18px; font-weight: bold;
      }
      QPushButton#noticeOk:hover { background: #1289f5; }
      QPushButton#noticeOk:pressed { background: #0754cc; }
      QPushButton#noticeOk:focus { border-color: #64e6ff; }
    """.replace("BACKGROUND", background))

    root = QVBoxLayout(msg)
    root.setContentsMargins(26, 25, 22, 23)
    root.setSpacing(20)
    content = QHBoxLayout()
    content.setSpacing(16)
    warning = QLabel()
    warning.setPixmap(load_icon("notice-warning").pixmap(54, 54))
    warning.setFixedSize(54, 54)
    content.addWidget(warning, alignment=Qt.AlignVCenter)
    divider = QFrame()
    divider.setObjectName("noticeDivider")
    divider.setFixedSize(2, 58)
    content.addWidget(divider, alignment=Qt.AlignVCenter)

    copy = QVBoxLayout()
    copy.setSpacing(5)
    labels = []
    for name, value in (("noticeHeading", heading or title), ("noticeText", text)):
      label = QLabel(value)
      label.setObjectName(name)
      label.setTextFormat(Qt.PlainText)
      label.setWordWrap(True)
      label.setTextInteractionFlags(Qt.TextSelectableByMouse)
      copy.addWidget(label)
      labels.append(label)
    content.addLayout(copy, 1)
    root.addLayout(content, 1)

    ok = QPushButton("  OK")
    ok.setObjectName("noticeOk")
    ok.setAccessibleName("OK")
    ok.setIcon(load_icon("notice-check"))
    ok.setIconSize(QSize(20, 20))
    ok.setFixedSize(112, 42)
    ok.setDefault(True)
    ok.setCursor(Qt.PointingHandCursor)
    ok.clicked.connect(msg.accept)
    root.addWidget(ok, alignment=Qt.AlignRight)
    available_width = self.screen().availableGeometry().width()
    msg.setFixedWidth(min(560, available_width - 24))
    msg.ensurePolished()
    margins = root.contentsMargins()
    text_width = (
      msg.width() - margins.left() - margins.right()
      - warning.width() - divider.width() - 2 * content.spacing()
    )
    for label in labels:
      label.ensurePolished()
      label.setContentsMargins(0, 0, 10, 2)
      label.setFixedWidth(text_width)
      label.setMinimumHeight(label.heightForWidth(text_width))
    msg.adjustSize()
    ok.setFocus()
    msg.exec_()

  def draw_search_spinner(self):
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#69caff"), 2.5, Qt.SolidLine, Qt.RoundCap))
    painter.drawArc(QRectF(4, 4, 16, 16), self.spinner_angle * 16, 260 * 16)
    painter.end()
    self.button_search.setIcon(QIcon(pixmap))
    self.spinner_angle = (self.spinner_angle - 30) % 360

  def set_search_state(self, activity=None):
    self.set_activity(activity or "Ready")
    self.button_search.setEnabled(activity is None)
    self.button_start.setEnabled(activity is None and bool(self.info_files))
    if activity in ("Searching", "Loading files"):
      self.draw_search_spinner()
      self.search_animation.start()
    else:
      self.search_animation.stop()
      self.button_search.setText("Search")
      self.button_search.setIcon(load_icon("search"))

  @pyqtSlot(str)
  def search_failed(self, message):
    if self.closing_for_shutdown:
      return
    self.set_search_state()
    self.message_search("Search failed", message)

  def search(self):
    if not self.button_search.isEnabled():
      return
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
        self.search_url = self.url_line.text()
        self.set_search_state("Searching")
        self.start_search_worker(
          ArchiveOrg(None, None, self.search_url, self.cookies, None),
          "get_titles", "_titles", self.search_step_1,
        )

  def start_search_worker(self, worker, method, signal, callback):
    self._archive_org = worker
    thread = QThread(self)
    worker.moveToThread(thread)
    result = getattr(worker, signal)
    result.connect(callback)
    result.connect(thread.quit)
    worker.search_error.connect(self.search_failed)
    worker.search_error.connect(thread.quit)
    thread.started.connect(getattr(worker, method))
    thread.finished.connect(self.finish_shutdown)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()

  @pyqtSlot(list)
  def search_step_1(self, list_files):
    if self.closing_for_shutdown:
      return
    if not list_files:
      self.search_failed("No files or folders were found at this URL.")
      return
    self.set_search_state("Select files")
    self.list_files = list_files
    self.name = "Found"
    self.check_list = ChecklistDialog(self)
    if self.check_list.exec_() != QDialog.Accepted:
      self.set_search_state()
      return

    self.set_search_state("Loading files")
    self.start_search_worker(
      ArchiveOrg(None, None, self.search_url, self.cookies, self.check_list.choices),
      "get_directory", "_ql", self.search_step_2,
    )

  @pyqtSlot(list)
  def search_step_2(self, ql):
    if self.closing_for_shutdown:
      return
    q = Queue()
    for index, (url, destination, filename, _) in enumerate(ql):
      q.put((filename, destination, url, index))
    self.start_search_worker(
      ArchiveOrg(None, None, None, self.cookies, q),
      "get_file_size", "_size", self.search_step_3,
    )

  def refresh_progress_bar(self, *args):
    pass

  @pyqtSlot(Queue)
  def search_step_3(self, out):
    if self.closing_for_shutdown:
      return
    self.table_widget.setRowCount(0)
    self.info_files = []
    self.download_sizes.clear()
    self.download_size_bytes.clear()
    self.download_stats.clear()
    self.count = 0
    while not out.empty():
      res = out.get()
      index = len(self.info_files)
      size_text = self.get_size_format(res[3]) if res[3] is not None else "--"
      self.download_sizes[index] = size_text
      self.download_size_bytes[index] = res[3] or 0
      self.add_row_table(res[0], size_text, index)
      self.info_files.append((*res[:4], index))
    self.finished = False
    self.set_search_state()
    self.update_dashboard()

  def hide_login(self):
    self.my_widget.setVisible(False)

  def select_start(self):
    if self.search_animation.isActive():
      return
    if not self.destination_path:
      self.message_search(
        "Information",
        "Please select a destination folder in the main window before starting your downloads.",
        heading="Download folder required",
      )
      return
    if not self.info_files:
      return
    if hasattr(self, "_downloader") and self._downloader.isRunning():
      return
    if self.finished:
      self.zero_progress()
    self._downloader = Downloader(
      self.cookies,
      self.concurrent_files.value(),
      self.info_files,
      self.destination_path,
      self
    )
    self._downloader.progress_updated.connect(self.insert_progress_bar_table)
    self._downloader.pause_changed.connect(self.refresh_file_pause)
    self._downloader.status_updated.connect(self.update_status)
    self._downloader.download_error.connect(self.show_download_error)
    self._downloader.finished.connect(self.button_start_finished)
    self.button_clean.setEnabled(False)
    self.button_remove_completed.setEnabled(False)
    self.button_search.setEnabled(False)
    self.button_start.setEnabled(False)
    self.set_activity("Downloading")
    self.button_pause.setEnabled(True)
    self.count = 0
    self.finished = True
    self.set_concurrent_files_enabled(False)
    self._downloader.start()

  def set_concurrent_files_enabled(self, enabled):
    self.concurrency_panel.setEnabled(enabled)
    self.concurrency_title.setEnabled(enabled)
    self.concurrency_opacity.setEnabled(not enabled)

  @pyqtSlot()
  def button_start_finished(self):
    self.set_concurrent_files_enabled(True)
    if self.closing_for_shutdown:
      self.finish_shutdown()
      return
    self.button_pause.setEnabled(False)
    self.button_resume.setEnabled(False)
    self.button_search.setEnabled(True)
    self.button_start.setEnabled(True)
    self.button_clean.setEnabled(True)
    self.button_remove_completed.setEnabled(True)
    self.set_activity("Ready")
    self.update_dashboard()

  def zero_progress(self):
    self.count = 0
    self.download_stats.clear()
    for index in range(len(self.info_files)):
      if self.table_widget.item(index, 2):
        self.set_status_text(index, "Queued")
      size = self.download_sizes.get(index, "--")
      if self.table_widget.item(index, 3):
        self.table_widget.item(index, 3).setText("0 B / " + size)
      if self.table_widget.item(index, 4):
        self.table_widget.item(index, 4).setText("--:--:-- / --:--:--")
      if self.table_widget.item(index, 5):
        self.table_widget.item(index, 5).setText("--")
      progress_bar = self.table_widget.cellWidget(index, 6).findChild(ProgressBar)
      if progress_bar:
        progress_bar.setValue(0)
        progress_bar.setObjectName("")
        progress_bar.style().unpolish(progress_bar)
        progress_bar.style().polish(progress_bar)
    self.update_dashboard()

  def select_clean(self):
    if hasattr(self, "_downloader") and self._downloader.isRunning():
      return
    self.info_files.clear()
    self.download_stats.clear()
    self.download_sizes.clear()
    self.download_size_bytes.clear()
    self.count = 0
    self.table_widget.setRowCount(0)
    self.button_start.setEnabled(False)
    self.update_dashboard()

  def select_folder(self):
    folder_path = QFileDialog.getExistingDirectory(self, "Select Folder", self.destination_path or os.path.expanduser("~"))
    if not folder_path:
      return
    self.destination_path = folder_path
    self.folder_path_label.setText(QFontMetrics(self.folder_path_label.font()).elidedText(folder_path, Qt.ElideMiddle, 400))
    self.folder_path_label.setToolTip(folder_path)

  def add_row_table(self, filename, size, index):
    filename = urllib.parse.unquote(filename)
    if index >= self.table_widget.rowCount():
      self.table_widget.setRowCount(index + 1)
    number = QTableWidgetItem(str(index + 1))
    number.setTextAlignment(Qt.AlignCenter)
    self.table_widget.setItem(index, 0, number)
    item = QTableWidgetItem(filename)
    item.setToolTip(filename)
    self.table_widget.setItem(index, 1, item)
    self.table_widget.setItem(index, 2, QTableWidgetItem("Queued"))
    self.set_status_text(index, "Queued")
    self.table_widget.setItem(index, 3, QTableWidgetItem("0 B / " + size))
    self.table_widget.setItem(index, 4, QTableWidgetItem("--:--:-- / --:--:--"))
    self.table_widget.setItem(index, 5, QTableWidgetItem("--"))
    progress_bar = ProgressBar(0)
    progress_cell = QWidget()
    progress_layout = QHBoxLayout(progress_cell)
    progress_layout.setContentsMargins(2, 0, 16, 0)
    progress_layout.addWidget(progress_bar)
    self.table_widget.setCellWidget(index, 6, progress_cell)
    open_button = QPushButton()
    open_button.setIcon(load_icon("folder"))
    open_button.setIconSize(QSize(24, 24))
    open_button.setObjectName("rowButton")
    open_button.setToolTip("Open destination folder")
    open_button.setFixedSize(37, 34)
    open_button.setCursor(Qt.PointingHandCursor)
    open_button.clicked.connect(lambda checked=False, row=index: self.open_file_folder(row))
    button_container = QWidget()
    button_layout = QHBoxLayout(button_container)
    button_layout.setContentsMargins(2, 2, 2, 2)
    button_layout.setAlignment(Qt.AlignCenter)
    button_layout.addWidget(open_button)
    more = QPushButton()
    more.setObjectName("rowButton")
    more.setIcon(load_icon("more"))
    more.setIconSize(QSize(20, 20))
    more.setFixedSize(37, 34)
    more.setToolTip("File actions")
    menu = QMenu(more)
    menu.addAction("Open destination folder", lambda: self.open_file_folder(index))
    menu.addAction("Copy filename", lambda: QApplication.clipboard().setText(filename))
    pause = menu.addAction("Pause download", lambda: self._downloader.set_file_paused(index, True))
    resume = menu.addAction("Resume download", lambda: self._downloader.set_file_paused(index, False))
    menu.aboutToShow.connect(lambda: self.update_file_pause_menu(index, pause, resume))
    pause.setVisible(False)
    resume.setVisible(False)
    more.setMenu(menu)
    button_layout.addWidget(more)
    self.table_widget.setCellWidget(index, 7, button_container)

  def update_file_pause_menu(self, index, pause, resume):
    downloader = getattr(self, "_downloader", None)
    available, _, paused, _ = downloader.file_pause_state(index) if downloader else (False,) * 4
    pause.setVisible(available and not paused)
    resume.setVisible(available and paused)

  @pyqtSlot(int)
  def refresh_file_pause(self, index):
    available, _, paused, active = self._downloader.file_pause_state(index)
    if not available:
      self.update_dashboard()
      return
    if paused:
      status = "Pausing…" if active else "Paused"
    else:
      status = "Downloading" if active else "Queued"
    self.set_status_text(index, status)
    if index in self.download_stats:
      self.download_stats[index]["status"] = status
    if not active:
      self.table_widget.item(index, 5).setText("--")
      timing = self.table_widget.item(index, 4)
      elapsed = timing.text().split(" / ", 1)[0]
      timing.setText(f"{elapsed} / --:--:--")
    self.update_dashboard()

  def open_file_folder(self, index):
    if not self.destination_path or not os.path.isdir(self.destination_path):
      return
    folder = self.destination_path
    if index < len(self.info_files):
      subfolder = self.info_files[index][1]
      candidate = os.path.join(folder, subfolder)
      if os.path.isdir(candidate):
        folder = candidate
    QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

  def create_progress_bar(self, _max):
    self.progress_bar = QProgressBar()
    self.progress_bar.setRange(0, _max)
    self.progress_bar.setValue(0)
    return self.progress_bar

  def create_table(self):
    self.table_widget = QTableWidget()
    self.table_widget.setColumnCount(8)
    self.table_widget.setRowCount(0)
    self.table_widget.setHorizontalHeaderLabels([
      "#", "           Filename", "Status", "Downloaded / Total", "Elapsed / Remaining", "Avg speed", "Progress", ""
    ])
    self.table_widget.setAlternatingRowColors(True)
    self.table_widget.setShowGrid(False)
    self.table_widget.verticalHeader().setVisible(False)
    self.table_widget.verticalHeader().setDefaultSectionSize(43)
    self.table_widget.setIconSize(QSize(30, 31))
    self.table_widget.setItemDelegateForColumn(1, FilenameDelegate(self.table_widget))
    self.table_widget.setEditTriggers(QAbstractItemView.NoEditTriggers)
    self.table_widget.setSelectionBehavior(QAbstractItemView.SelectRows)
    self.table_widget.setSelectionMode(QAbstractItemView.SingleSelection)
    self.table_widget.setContextMenuPolicy(Qt.CustomContextMenu)
    self.table_widget.customContextMenuRequested.connect(self.show_file_menu)
    self.table_widget.setWordWrap(False)
    header = self.table_widget.horizontalHeader()
    header.setSectionResizeMode(1, QHeaderView.Interactive)
    for col in (0, 2, 3, 4, 5, 6, 7):
      header.setSectionResizeMode(col, QHeaderView.Interactive)
    header.setMinimumSectionSize(40)
    header.setFixedHeight(46)
    for column in (1, 2, 3, 4, 5):
      self.table_widget.horizontalHeaderItem(column).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    self.table_widget.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    self.resize_table_columns()

  def resizeEvent(self, event):
    super().resizeEvent(event)
    if hasattr(self, "table_widget"):
      self.resize_table_columns()

  def resize_table_columns(self):
    widths = (65, 422, 175, 235, 221, 124, 174, 109)
    minimum = (47, 231, 145, 178, 174, 103, 116, 92)
    available = self.width() - 57
    scale = min(available / sum(widths), 1)
    sizes = [max(limit, round(width * scale)) for width, limit in zip(widths, minimum)]
    sizes[1] += max(0, available - sum(sizes))
    for column, width in enumerate(sizes):
      self.table_widget.setColumnWidth(column, width)


  def show_file_menu(self, position):
    row = self.table_widget.rowAt(position.y())
    if row < 0:
      return
    actions = self.table_widget.cellWidget(row, 7)
    menu = actions.findChild(QMenu) if actions else None
    if menu:
      menu.popup(self.table_widget.viewport().mapToGlobal(position))

  def set_status_text(self, index, status):
    item = self.table_widget.item(index, 2)
    if item is None:
      return
    item.setText("Completed" if status == "Done" else status)
    if status == "Queued":
      item.setToolTip("")
    filename = self.table_widget.item(index, 1)
    filename.setIcon(file_icon(filename.text(), status))
    icons = {"Queued": "status-queued", "Downloading": "status-active", "Done": "status-done"}
    item.setIcon(load_icon(icons[status]) if status in icons else QIcon())
    colors = {
      "Queued": "#a6b6c9", "Downloading": "#429dff",
      "Building file": "#FBBF24", "Done": "#60bd59", "Failed": "#F87171"
    }
    item.setForeground(QColor(colors.get(status, "#E2E8F0")))
    font = item.font()
    font.setBold(True)
    item.setFont(font)

  @pyqtSlot(int, str)
  def show_download_error(self, index, message):
    item = self.table_widget.item(index, 2)
    if item:
      item.setToolTip(message)

  @pyqtSlot(int, int)
  def update_status(self, index, option):
    if option == 1:
      self.set_status_text(index, "Building file")
      if index in self.download_stats:
        self.download_stats[index]["status"] = "Building file"
    elif option == 2:
      progress_bar = self.table_widget.cellWidget(index, 6).findChild(ProgressBar)
      if progress_bar:
        progress_bar.setValue(100)
        progress_bar.setObjectName("completedBar")
        progress_bar.style().unpolish(progress_bar)
        progress_bar.style().polish(progress_bar)
      self.set_status_text(index, "Done")
      if self.download_size_bytes.get(index):
        size = self.download_sizes[index]
        self.table_widget.item(index, 3).setText(f"{size} / {size}")
      if index in self.download_stats:
        self.download_stats[index]["status"] = "Done"
      self.count += 1
    elif option == 3:
      self.set_status_text(index, "Failed")
      bar = self.table_widget.cellWidget(index, 6).findChild(ProgressBar)
      if bar:
        bar.setObjectName("failedBar")
        bar.style().unpolish(bar)
        bar.style().polish(bar)
      if index in self.download_stats:
        self.download_stats[index]["status"] = "Failed"
    self.update_dashboard()

  def update_row(self, upload_size, _time, speed, progress, index, status, remaining):
    downloader = getattr(self, "_downloader", None)
    if downloader and downloader.isRunning():
      _, _, paused, active = downloader.file_pause_state(index)
      if paused:
        status = "Pausing…" if active else "Paused"
        if not active:
          speed, remaining = "--", None
    self.set_status_text(index, status)
    size = self.download_sizes.get(index, "--")
    self.table_widget.item(index, 3).setText(self.get_size_format(upload_size) + " / " + size)
    elapsed_text = self.format_time(_time)
    remaining_text = self.format_time(remaining) if remaining is not None else "--:--:--"
    self.table_widget.item(index, 4).setText(f"{elapsed_text} / {remaining_text}")
    self.table_widget.item(index, 5).setText(self.get_size_format(speed) + "/s" if isinstance(speed, (float, int)) else str(speed))
    progress_bar = self.table_widget.cellWidget(index, 6).findChild(ProgressBar)
    if progress_bar:
      progress_bar.setValue(progress)
    self.download_stats[index] = {
      "bytes": upload_size, "speed": speed if isinstance(speed, (float, int)) else 0,
      "remaining": remaining, "status": status
    }
    self.update_dashboard()

if __name__ == "__main__":
  app = QApplication(sys.argv)
  icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iad_icon.ico")
  if os.path.isfile(icon_path):
    app.setWindowIcon(QIcon(icon_path))
  QApplication.setStyle(QStyleFactory.create("Fusion"))
  app.setFont(QFont("Segoe UI", 10))
  app.setStyleSheet(Main.get_theme(None))
  login_window = WLogin()
  login_window.show()
  sys.exit(app.exec_())
