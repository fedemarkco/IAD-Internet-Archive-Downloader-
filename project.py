from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from operator import itemgetter
from threading import Thread
from queue import Queue

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


sem = threading.Semaphore()
urllib3.disable_warnings()

class Archive_org(QObject):
  _response = pyqtSignal(object)
  _titles = pyqtSignal(list)
  _ql = pyqtSignal(list)
  _size = pyqtSignal(Queue)

  def __init__(self, user=None, password=None, url=None, cookies=None, searchs=None, parent=None):
    super(Archive_org, self).__init__(parent)
    self.url = url
    self.user = user
    self.password = password
    self.cookies = cookies
    self.searchs = searchs
    self.k = 0
    self.qq = Queue()
    self.ql = []
    self.PATHD = os.path.dirname(__file__) + '\\'

  def login(self):
    urlLogin = "https://archive.org/account/login"
    sourceLogin = requests.get(urlLogin)
    sourcePost = requests.post(
      urlLogin, 
      cookies=sourceLogin.cookies, 
      headers = {
        "Referer": "https://archive.org/account/login"
      }, 
      data = {
        "username": self.user, 
        "password": self.password, 
        "remember": "true", 
        "referer": "https://archive.org/", 
        "login": "true", 
        "submit_by_js": "true"
      }
    )
    self._response.emit(sourcePost)

  def strip_tags(self, value):
    return re.compile(r'<[^<]*?/?>').sub("", value)

  def getFileSize(self):
    q = self.searchs
    out = Queue()
    num_threads = min(200, q.qsize())
    for i in range(num_threads):
      worker = Thread(target=self.getFileSizeT, args=(q, out,))
      worker.setDaemon(True)
      worker.start()

    q.join()

    self._size.emit(out)

  def getFS(self, u):
    while True:
      try:
        source = requests.get(u[2], cookies = self.cookies, stream=True)
        if source.status_code == 200:
          break
      except:
        time.sleep(0.5)
    try:
      length = int(source.headers["Content-Length"])
    except:
      length = None
    return (u[0], u[1], u[2], length, u[3])

  def getFileSizeT(self, q, out):
    while not q.empty():
      u = q.get()
      try:
        o = self.getFS(u)
        out.put((o[0], o[1], o[2], o[3], o[4]))
        q.task_done()
      except:
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        q.put(u)
        q.task_done()

  def getA(self, cookies):
    self.getD(cookies, self.url)
    for _ql in self.ql:
      self.qq.put(_ql)

  def getTitles(self):
    source = requests.get(self.url, cookies=self.cookies)
    searchs = re.findall('<tr.*?>\s+<td><a\shref="(.*?)">(.*?)<\/a>', source.text)[1:]
    self._titles.emit(searchs)

  def getD(self):
    self.getD2(self.searchs, self.cookies, self.url)
    self._ql.emit(self.ql)

  def getD2(self, searchs, cookies, urlTemp='', folders='', listFilesD=[], listFilesURL=[]):
    source = requests.get(urlTemp, cookies=cookies)
    if self.k == 0:
      self.k = 1
      searchTemp = []
      for m in searchs:
        if m[1][-1] != "/":
          if urlTemp[-1] != "/":
            urlTemp += "/"
          self.ql.append((urlTemp+m[0], '', m[1], cookies))
        else:
          searchTemp.append(m)
      searchs = searchTemp
    else:
      searchs = re.findall('<tr.*?>\s+<td><a\shref="(.*?)">(.*?)<\/a>', source.text)

    for d in range(len(searchs)):
      if "to parent directory" in searchs[d][1]:
        continue
      u = searchs[d][0] #url
      f = searchs[d][1] #filename
      if urlTemp[-1] != "/":
        urlTemp += "/"
      if f[-1] == "/":
        self.getD2(searchs, cookies, urlTemp+u, folders + f[:-1] + '\\')
      else:
        files = re.findall('<tr.*?>\s+<td><a\shref="(.*?)">.*?<\/a>', source.text)[1:]
        for f in files:
          if (urlTemp+f, folders, f, cookies) in self.ql:
            continue
          self.ql.append((urlTemp+f, folders, f, cookies))

class Downloader(QThread):
  _createProgressBar = pyqtSignal(list)
  _updateStatus = pyqtSignal(int, int)
  _finished = pyqtSignal()

  def __init__(self, cookies, threads, infoFiles, dest, parent=None):
    super(Downloader, self).__init__(parent)
    self.cookies = cookies
    self.num_threads = threads
    self.infoFiles = infoFiles
    self.dest = dest
    self.chunk_size = 512000
    self.speedFile = []
    self.TotalThreads = 32

  def run(self):
    self.TotalThreadsQueue = Queue()
    self.ql = []
    self.qIn = Queue()

    for l in self.infoFiles:
      self.qIn.put(l)

    for tt in range(self.TotalThreads):
      self.TotalThreadsQueue.put(1)

    self.num_threads = min (self.num_threads, len(self.infoFiles))
    for i in range(self.num_threads):
      worker = Thread(target=self.downlT, args=(self.qIn,))
      worker.start()

    self.qIn.join()
    self._finished.emit()

    if os.path.isdir("__pycache__"):
      shutil.rmtree("__pycache__")

  def arrancar(self, q, url, dest, filesize, index, start, start_time):
    while not q.empty():
      u = q.get()
      try:
        self.getFile(u[0], u[1], u[2], url, dest, filesize, index, start, start_time)
      except:
        q.put(u)
      q.task_done()

  def createFile(self, dest, filename, index):
    k = 0
    for k in range(len(self.speedFile)):
      if self.speedFile[k]["id"] == index:
        break

    self.speedFile[k]["body"].sort()
    self._updateStatus.emit(index, 1)

    wfd = open(os.path.join(dest, filename), "wb")
    for c in self.speedFile[k]["body"]:
      fd = open(os.path.join(dest, filename+'-'+str(c[0])+str(c[1])), "rb")
      shutil.copyfileobj(fd, wfd)
      fd.close()
    wfd.close()

    for c in self.speedFile[k]["body"]:
      os.remove(os.path.join(dest, filename+'-'+str(c[0])+str(c[1])))

    self._updateStatus.emit(index, 2)

  def writeFragment(self, dest, filename, fragment):
    f = open(os.path.join(dest, filename), "wb")
    f.write(fragment)
    f.close()

  def getFile(self, a, b, filename, url, dest, filesize, index, start, start_time):
    k = 0
    for k in range(len(self.speedFile)):
      if self.speedFile[k]["id"] == index:
        break

    if filesize is not None:
      r = requests.get(
        url, 
        headers = {"Range": "bytes=%d-%d" % (a, b)}, 
        cookies = self.cookies, 
        verify = False, 
        stream = True
      )
    else:
      r = requests.get(url, cookies = self.cookies, verify = False, stream = True)

    for chunk in r.iter_content(chunk_size=self.chunk_size):
      speed = 0
      self.speedFile[k]["speed"] += len(chunk)

      if filesize is not None:
        self.speedFile[k]["perc"] += 1
        percentage = self.speedFile[k]["perc"]/self.speedFile[k]["chunks"]
        self._createProgressBar.emit(
          [self.speedFile[k]["speed"], 
          round(time.perf_counter() - start), 
          self.speedFile[k]["speed"]/(time.perf_counter() - start), 
          int(percentage*100), 
          index, 
          "Downloading"]
        )
      else:
        self._createProgressBar.emit(
          [self.speedFile[k]["speed"], 
          round(time.perf_counter() - start), 
          "-", 
          0, 
          index, 
          "Downloading"]
        )

      self.speedFile[k]["body"].append(
        (a, self.speedFile[k]["speed"], filename + "-" + str(a))
      )
      self.writeFragment(
        dest, 
        filename + "-" + str(a) + str(self.speedFile[k]["speed"]), 
        chunk
      )
      self.TotalThreadsQueue.put(1)

  def prepareDownload(self, filename, dest, url, filesize, index):
    q = Queue()

    if filename.strip() == "":
      filename = os.path.basename(url)

    sem.acquire()

    dest = dest.replace("\\", "/")

    if dest.strip() != "":
      if not os.path.isdir(dest):
        os.makedirs(dest)

    self.speedFile.append(
      {
        "id": index, 
        "url": url, 
        "filename": filename, 
        "speed": 0, 
        "perc": 0, 
        "chunks": 0, 
        "body": []
      }
    )

    res = []

    if filesize is not None:
      if filesize <= 3000000:
        trunks = 1
      else:
        trunks = math.ceil(filesize/3000000)

      size = math.ceil(filesize/trunks)
      chunks = math.ceil(filesize/self.chunk_size)
      i = 0
      while True:
        start = size * i
        if (start + size) >= filesize:
          end = filesize
          q.put((start, end-1, filename))
          self.speedFile[len(self.speedFile)-1]["chunks"] += math.ceil((end-start)/self.chunk_size)
          break
        else:
          end = start + size
          q.put((start, end-1, filename))
          self.speedFile[len(self.speedFile)-1]["chunks"] += math.ceil((end-start)/self.chunk_size)
        i += 1
    else:
      q.put((0, 0, filename))
    sem.release()

    start = time.perf_counter()
    start_time = time.time()

    for k in range(len(self.speedFile)):
      if self.speedFile[k]["id"] == index:
        break

    while not q.empty():
      if not self.TotalThreadsQueue.empty():
        self.TotalThreadsQueue.get()
        worker = Thread(target=self.arrancar, args=(q, url, dest, filesize, index, start, start_time,))
        worker.setDaemon(True)
        worker.start()

    q.join()

    self.createFile(dest, filename, index)

  def downlT(self, q):
    while not q.empty():
        u = q.get()
        self.prepareDownload(
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
    self.pressButton = False
    self.name = self.parent.name
    self.icon = None
    self.model = QStandardItemModel()
    self.listView = QListView()
    stringlist = self.parent.listFiles
    checked = True

    for string in stringlist:
      item = QStandardItem(string[1])
      item.setEditable(False)
      item.setCheckable(True)
      check = \
        (Qt.Checked if checked else Qt.Unchecked)
      item.setCheckState(check)
      self.model.appendRow(item)

    self.listView.setModel(self.model)
    self.model.itemChanged.connect(self.click2)
    self.okButton = QPushButton("OK")
    self.selectButton = QPushButton("Select All")
    self.unselectButton = QPushButton("Unselect All")

    hbox = QHBoxLayout()
    hbox.addStretch(1)
    hbox.addWidget(self.okButton)
    hbox.addWidget(self.selectButton)
    hbox.addWidget(self.unselectButton)

    vbox = QVBoxLayout(self)
    vbox.addWidget(self.listView)
    vbox.addStretch(1)
    vbox.addLayout(hbox)

    self.setWindowTitle(self.name)

    if self.icon:
      self.setWindowIcon(self.icon)

    self.okButton.clicked.connect(self.onAccepted)
    self.selectButton.clicked.connect(self.select)
    self.unselectButton.clicked.connect(self.unselect)

    self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

    self.setFixedSize(280, 260)

  def strip_tags(self, value):
    return re.compile(r'<[^<]*?/?>').sub("", value)

  def onAccepted(self):
    self.choices = [self.model.item(i).text() for i in
                    range(self.model.rowCount())
                    if self.model.item(i).checkState()
                    == Qt.Checked]
    self.accept()

  def click2(self, item):
    if self.pressButton:
      self.listView.setCurrentIndex(item.index())

  def select(self):
    self.pressButton = False
    for i in range(self.model.rowCount()):
      item = self.model.item(i)
      item.setCheckState(Qt.Checked)
    self.pressButton = True

  def unselect(self):
    self.pressButton = False
    for i in range(self.model.rowCount()):
      item = self.model.item(i)
      item.setCheckState(Qt.Unchecked)
    self.pressButton = True

class WLogin(QDialog):
  def __init__(self, parent=None):
    super(WLogin, self).__init__(parent)

    self.setFixedWidth(600)
    self.setFixedHeight(80)

    self.message = QLabel("")
    self.message.setStyleSheet("color: red;")

    self.user = QLineEdit("")
    self.user.setPlaceholderText("user")
    self.passw = QLineEdit("")
    self.passw.setPlaceholderText("password")
    self.passw.setEchoMode(QLineEdit.Password)
    self.buttonLogin = QPushButton("Login")

    log = QHBoxLayout()
    log.addWidget(self.user)
    log.addWidget(self.passw)
    log.addWidget(self.buttonLogin)

    messageWidget = QVBoxLayout()
    messageWidget.addWidget(self.message)

    loginLayout = QGridLayout()
    loginLayout.addLayout(log, 0, 0, 5, 78)
    loginLayout.addLayout(messageWidget, 6, 31, 1, 10)
    self.setLayout(loginLayout)

    self.buttonLogin.clicked.connect(self.login)

    self.setWindowTitle("IAD (Internet Archive Downloader) - Login")

    self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)

    QApplication.setStyle(QStyleFactory.create("Fusion"))

  def login(self):
    out = Queue()
    self.buttonLogin.setEnabled(False)
    self.message.setText("Verifying...")
    self.message.repaint()
    self._archive_org = Archive_org(self.user.text(), self.passw.text(), None, None, None)
    thread = QThread(self)
    self._archive_org.moveToThread(thread)
    self._archive_org._response.connect(self.loginT)
    thread.started.connect(self._archive_org.login)
    thread.start()
  
  @pyqtSlot(object)
  def loginT(self, response):
    message = response.text
    self.cookies = response.cookies

    if "successful login" in str(message).lower():
      self.message.setText("Success")
      self.message.repaint()
      self.accept()
      _main = Main(self)
      _main.show()
    else:
      self.message.setText("Failed")
      self.message.repaint()
    self.buttonLogin.setEnabled(True)

class ProgressBar(QProgressBar):
  def __init__(self, value, parent=None):
    QProgressBar.__init__(self)
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

    self.infoFiles = []
    self.count = 0
    self.finished = False

    self.urlLine = QLineEdit("")

    urlLabel = QLabel("&URL:")
    urlLabel.setBuddy(self.urlLine)

    self.buttonSearch = QPushButton("Search")

    self.searchingLabel = QLabel("")
    self.searchingLabel.setStyleSheet("color: red;")

    WsearchLabel = QVBoxLayout()
    WsearchLabel.addWidget(self.searchingLabel)

    threadLabel = QLabel("&Thread:")
    self.threadComboBox = QComboBox()
    self.threadComboBox.addItems(["1", "2", "4", "8", "16", "32"])
    threadLabel.setBuddy(self.threadComboBox)

    self.fpath = QLabel("")

    buttonFolder = QPushButton("Save To Folder")

    self.buttonStart = QPushButton("Start")
    self.buttonStart.setEnabled(False)
    self.buttonClean = QPushButton("Clean")
    buttonExit = QPushButton("Exit")

    self.createTable()

    table = QVBoxLayout()
    table.addWidget(self.tableWidget)

    topLayout2 = QHBoxLayout()
    topLayout2.addWidget(threadLabel)
    topLayout2.addWidget(self.threadComboBox)
    topLayout2.addWidget(buttonFolder)

    topLayout = QHBoxLayout()
    topLayout.addWidget(urlLabel)
    topLayout.addWidget(self.urlLine)
    topLayout.addWidget(self.buttonSearch)

    topLayout3 = QHBoxLayout()
    topLayout3.addWidget(self.fpath)

    buttonStartClean = QHBoxLayout()
    buttonStartClean.addWidget(self.buttonStart)
    buttonStartClean.addWidget(self.buttonClean)

    buttonEx = QHBoxLayout()
    buttonEx.addWidget(buttonExit)

    mainLayout = QGridLayout()
    mainLayout.addLayout(topLayout, 0, 0, 2, 78)
    topLayout.setContentsMargins(0, 15, 0, 40)
    mainLayout.addLayout(WsearchLabel, 1, 30, 1, 10)
    mainLayout.addLayout(topLayout2, 2, 0, 1, 1)
    mainLayout.addLayout(topLayout3, 2, 1, 1, 70)
    mainLayout.addLayout(table, 3, 0, 1, 78)
    mainLayout.addLayout(buttonStartClean, 4, 0, 1, 30)
    mainLayout.addLayout(buttonEx, 4, 63, 1, 15)
    table.setContentsMargins(0, 30, 0, 0)
    self.setLayout(mainLayout)

    self.buttonSearch.clicked.connect(self.search)
    buttonFolder.clicked.connect(self.selectFolder)
    self.buttonClean.clicked.connect(self.selectClean)
    self.buttonStart.clicked.connect(self.selectStart)
    buttonExit.clicked.connect(self.close)

    self.setWindowTitle("IAD (Internet Archive Downloader)")

    self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)

    QApplication.setStyle(QStyleFactory.create("Fusion"))

  def mousePressEvent(self, e):
    self.tableWidget.clearSelection()

  def get_size_format(self, b, factor=1024, suffix="B"):
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
      if b < factor:
        return f"{b:.2f} {unit}{suffix}"
      b /= factor
    return f"{b:.2f}Y {suffix}"

  @pyqtSlot(list)
  def insertProgressBarTable(self, values):
    self.updateRow(values[0], values[1], values[2], values[3], values[4], values[5])

  def checkUrl(self, url):
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

  def messageSearch(self, title, text):
    msg = QMessageBox(self)
    msg.setWindowTitle(title)
    msg.setIcon(QMessageBox.Warning)
    msg.setText(text)
    msg.exec_()

  def search(self):
    _type = self.checkUrl(self.urlLine.text())
    if isinstance(_type, str):
      if _type == "V":
        self.messageSearch("Wrong url", "The URL cannot be empty")
      if _type == "E":
        self.messageSearch("Wrong url", "The URL should be as follows:\nhttps://archive.org/download/example")
    if isinstance(_type, bool):
      if not _type:
        self.messageSearch("Wrong url", "The URL should be as follows:\nhttps://archive.org/download/example")
      else:
        self.buttonSearch.setEnabled(False)
        self.searchingLabel.setText("Searching...")
        self.searchingLabel.repaint()
        self._archive_org = Archive_org(None, None, self.urlLine.text(), self.cookies, None)
        thread = QThread(self)
        self._archive_org.moveToThread(thread)
        self._archive_org._titles.connect(self.searchT)
        thread.started.connect(self._archive_org.getTitles)
        thread.start()

  @pyqtSlot(list)
  def searchT(self, listFiles):
    q, out = Queue(), Queue()
    info, searchs, self.infoFiles = [], [], []
    self.listFiles = listFiles
    self.name = "Found"
    self.searchingLabel.setText("")
    self.searchingLabel.repaint()
    self.checkList = ChecklistDialog(self)
    url = self.urlLine.text()

    if url[-1] != "/":
      url += "/"

    if self.checkList.exec_() == QDialog.Accepted:
      self.selectClean()
      for s in self.checkList.choices:
        for k in self.listFiles:
          if k[1] == s:
            searchs.append(k)
      if len(searchs) > 0:
        self.searchingLabel.setText("Getting links...")
        self.searchingLabel.repaint()
        self._archive_org.k = 0
        self._archive_org.ql.clear()
        self._archive_org = Archive_org(None, None, self.urlLine.text(), self.cookies, searchs)
        thread = QThread(self)
        self._archive_org.moveToThread(thread)
        self._archive_org._ql.connect(self.searchT2)
        thread.started.connect(self._archive_org.getD)
        thread.start()

    self.buttonSearch.setEnabled(True)

  @pyqtSlot(list)
  def searchT2(self, ql):
    q = Queue()
    index = 0

    for u in ql:
      urlFile = u[0]
      dest = u[1]
      filename = u[2]
      q.put((filename, dest, urlFile, index))
      index += 1

    self._archive_org = Archive_org(None, None, None, self.cookies, q)
    thread = QThread(self)
    self._archive_org.moveToThread(thread)
    self._archive_org._size.connect(self.searchT3)
    thread.started.connect(self._archive_org.getFileSize)
    thread.start()

  def refreshProgressBar(self):
    if len(self.infoFiles) > 0:
      progressValue = self.tableWidget.cellWidget(0, 5).value()
      self.tableWidget.removeCellWidget(0, 5)
      progressBar = ProgressBar(progressValue)
      self.tableWidget.setCellWidget(0, 5, progressBar)
  
  @pyqtSlot(Queue)
  def searchT3(self, out):
    if out.qsize() <= 15:
      self.tableWidget.setRowCount(15)
      for row in range(self.tableWidget.rowCount()):
        self.tableWidget.setRowHeight(row, 10)
    else:
      self.tableWidget.setRowCount(out.qsize())

    size = out.qsize()

    while not out.empty():
      res = out.get()
      if res[3] is not None:
        self.addRowTable(res[0], self.get_size_format(res[3]), res[4])
      else:
        self.addRowTable(res[0], '-', res[4])
      self.infoFiles.append((res[0], res[1], res[2], res[3], res[4]))

    self.searchingLabel.setText("")
    self.searchingLabel.repaint()

    self.setWindowTitle("IAD (Internet Archive Downloader) - 0/{0} files".format(size))

    self.buttonStart.setEnabled(True)
    self.finished = False

  def hideLogin(self):
    self.myWidget.setVisible(False)

  def selectStart(self):
    if self.fpath.text() == "":
      msg = QMessageBox(self)
      msg.setWindowTitle("Information")
      msg.setIcon(QMessageBox.Warning)
      msg.setText("You must select the folder where\nthe downloaded files will be saved.")
      msg.exec_()
    else:

      if self.finished:
        self.zeroProgress()

      self.infoFiles = sorted(self.infoFiles, key=itemgetter(2))
      self._downloader = Downloader(
        self.cookies, 
        int(self.threadComboBox.currentText()), 
        self.infoFiles, 
        self.fpath.text(), 
        self
      )
      self._downloader._createProgressBar.connect(self.insertProgressBarTable)
      self._downloader._updateStatus.connect(self.updateStatus)
      self._downloader.start()
      self.buttonClean.setEnabled(False)
      self.buttonSearch.setEnabled(False)
      self.buttonStart.setEnabled(False)
      self._downloader._finished.connect(self.butSt)

      self.count = 0
      self.finished = True

  @pyqtSlot()
  def butSt(self):
    self.buttonSearch.setEnabled(True)
    self.buttonStart.setEnabled(True)
    self.buttonClean.setEnabled(True)

  def zeroProgress(self):
    size = len(self.infoFiles)
    self.setWindowTitle("IAD (Internet Archive Downloader) - 0/{0} files".format(size))

    for index in range(len(self.infoFiles)):
      self.tableWidget.setItem(index, 1, QTableWidgetItem(""))
      size = re.search('.*?\/(.*)', self.tableWidget.item(index, 2).text())
      size = size.group(1).strip()
      self.tableWidget.setItem(index, 2, QTableWidgetItem("0 / "+size))
      self.tableWidget.setItem(index, 3, QTableWidgetItem(""))
      self.tableWidget.setItem(index, 4, QTableWidgetItem(""))
      progressBar = self.tableWidget.cellWidget(index, 5)
      progressBar.setValue(0)

  def selectClean(self):
    self.infoFiles.clear()
    self.tableWidget.clearContents()
    self.tableWidget.setRowCount(15)
    self.buttonStart.setEnabled(False)

  def selectFolder(self):
    dialog = QFileDialog(self)
    folderPath = dialog.getExistingDirectory(self, "Select Folder")
    if self.fpath.text() != '' and folderPath == '':
      folderPath = self.fpath.text()
    if len(folderPath) > 60:
      folderPath = folderPath[:60]+'...'
    self.fpath.setText(folderPath)

  def addRowTable(self, filename, size, index):
    filename = urllib.parse.unquote(filename)
    self.tableWidget.setItem(index, 0, QTableWidgetItem(filename))

    if size != '':
      self.tableWidget.setItem(index, 2, QTableWidgetItem("0 / "+size))
    else:
      self.tableWidget.setItem(index, 2, QTableWidgetItem("-"))

    progressBar = ProgressBar(0)

    self.tableWidget.setCellWidget(index, 5, progressBar)
    self.tableWidget.setRowHeight(index, 10)

  def createProgressBar(self, _max):
    self.progressBar = QProgressBar()
    self.progressBar.setRange(0, _max)
    self.progressBar.setValue(0)
    return self.progressBar

  def createTable(self):
    self.tableView = QHeaderView(Qt.Horizontal)
    self.tableView.sectionResized.connect(self.refreshProgressBar)
    self.tableWidget = QTableWidget()
    self.setFixedHeight(496)
    self.tableWidget.setRowCount(15)
    self.tableWidget.setColumnCount(6)
    self.tableWidget.setHorizontalHeaderLabels(
      [
        "Filename", 
        "Status", 
        "Uploaded / Size", 
        "Time", 
        "Speed", 
        "Progress"
      ]
    )
    self.tableWidget.verticalHeader().setVisible(False)

    for row in range(self.tableWidget.rowCount()):
      self.tableWidget.setRowHeight(row, 10)

    self.tableWidget.setHorizontalHeader(self.tableView)
    self.tableWidget.horizontalHeader().setStretchLastSection(True)
    self.tableWidget.setColumnWidth(1, 78);
    self.tableWidget.setColumnWidth(2, 130);
    self.tableWidget.setColumnWidth(3, 55);
    self.tableWidget.setColumnWidth(4, 75);
    self.tableWidget.setEditTriggers(self.tableWidget.NoEditTriggers)
    
  @pyqtSlot(int, int)
  def updateStatus(self, index, option):
    if option == 1:
      self.tableWidget.setItem(index, 1, QTableWidgetItem("Building File"))
    if option == 2:
      progressBar = self.tableWidget.cellWidget(index, 5).setValue(100)
      self.tableWidget.setItem(index, 1, QTableWidgetItem("Done"))
      self.count += 1
      title = self.windowTitle()
      title = re.sub('\s\d+', ' '+str(self.count), title)
      self.setWindowTitle(title)

  def updateRow(self, uploadSize, _time, speed, progress, index, status):
    self.tableWidget.setItem(index, 1, QTableWidgetItem(status))

    size = self.tableWidget.item(index, 2).text().split("/")[1].strip()
    self.tableWidget.setItem(index, 2, QTableWidgetItem(self.get_size_format(uploadSize) + " / " + size))

    self.tableWidget.setItem(index, 3, QTableWidgetItem(str(datetime.timedelta(seconds=_time))))

    if speed != "-":
      self.tableWidget.setItem(index, 4, QTableWidgetItem(self.get_size_format(speed) + "/s"))
    else:
      self.tableWidget.setItem(index, 4, QTableWidgetItem(speed))

    progressBar = self.tableWidget.cellWidget(index, 5)
    if progressBar.value() < progress:
      progressBar.setValue(progress)

if __name__ == "__main__":
    app = QApplication([])
    gallery = WLogin()
    gallery.show()
    sys.exit(app.exec_())
