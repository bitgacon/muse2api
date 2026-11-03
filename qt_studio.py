"""Qt desktop presentation reusing the existing studio operations and state."""
import csv
import base64
import html
import json
import mimetypes
from pathlib import Path
import queue
import time

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QPixmap, QIcon, QColor, QPainter, QKeySequence, QShortcut, QTextCursor, QFontDatabase, QFont
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTabWidget, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox,
    QLineEdit, QCheckBox, QGroupBox, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QListWidget, QListWidgetItem, QAbstractItemView, QFileDialog, QMessageBox,
    QInputDialog, QApplication, QSplitter, QScrollArea, QDialog, QStyledItemDelegate)

import desktop_gui_tk as core
from qt_media import MediaViewer, ThumbnailQueue, VIDEO_EXTENSIONS
from video_references import validate_references

THEME = '''
QMainWindow,QDialog,QWidget { background:#0a0e17; color:#e7eaf3; font:10pt "Segoe UI"; }
QLabel { background:transparent; }
QLabel#heading { font-size:20px; font-weight:650; color:#e7eaf3; }
QLabel#muted { color:#8590a8; }
QLabel#warning { color:#fbbf24; }
QLabel#viewerTitle { font-weight:600; padding:5px; }
QLabel#mediaCanvas { background:#080c14; border:1px solid #20293d; border-radius:10px; }
QTabWidget::pane { border:1px solid #20293d; background:#121a2b; border-radius:10px; }
QTabBar::tab { background:#0e1421; color:#8590a8; border:1px solid #20293d; padding:10px 15px; margin-right:3px; }
QTabBar::tab:selected { color:#e7eaf3; background:#18263e; border-bottom:2px solid #4f8cff; }
QTabBar::tab:hover { color:#9ed5ff; }
QGroupBox { background:#121a2b; border:1px solid #20293d; border-radius:12px; margin-top:15px; padding:15px 10px 10px; }
QGroupBox::title { subcontrol-origin:margin; left:14px; color:#9ed5ff; padding:0 4px; }
QPushButton { background:#18263e; color:#c4d8f7; border:1px solid #2b3550; border-radius:7px; padding:8px 13px; }
QPushButton:hover { border-color:#4f8cff; background:#213353; }
QPushButton:pressed { background:#2563eb; }
QPushButton:disabled { color:#52617c; background:#111b2c; }
QPushButton[primary="true"] { background:#2563eb; color:white; border:1px solid #4f8cff; font-weight:600; }
QPushButton[primary="true"]:hover { background:#4f8cff; }
QLineEdit,QPlainTextEdit,QTextBrowser,QComboBox,QSpinBox { background:#0e1421; color:#e7eaf3; border:1px solid #2b3550; border-radius:7px; padding:7px; selection-background-color:#2563eb; }
QLineEdit:focus,QPlainTextEdit:focus,QComboBox:focus { border-color:#4f8cff; }
QComboBox::drop-down { border:0; width:22px; }
QComboBox QAbstractItemView { background:#18263e; selection-background-color:#2563eb; }
QCheckBox { background:transparent; spacing:8px; padding:4px; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid #42516b; border-radius:4px; background:#0e1421; }
QCheckBox::indicator:checked { background:#4f8cff; border:1px solid #9ed5ff; }
QTreeWidget,QListWidget { background:#0e1421; alternate-background-color:#121a2b; color:#e7eaf3; border:1px solid #20293d; border-radius:8px; padding:5px; }
QTreeWidget::item { padding:5px; }
QTreeWidget::item:selected,QListWidget::item:selected { background:#213b66; color:white; }
QListWidget::item { padding:6px; border:1px solid #20293d; border-radius:9px; margin:4px; }
QHeaderView::section { background:#18263e; color:#8590a8; padding:7px; border:0; border-right:1px solid #20293d; }
QSlider::groove:horizontal { height:5px; border-radius:2px; background:#2b3550; }
QSlider::sub-page:horizontal { background:#4f8cff; }
QSlider::handle:horizontal { background:#9ed5ff; border:1px solid #4f8cff; width:12px; margin:-4px 0; border-radius:6px; }
QScrollBar:vertical { background:#0e1421; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#334664; min-height:25px; border-radius:4px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QScrollArea { border:0; }
QSplitter::handle { background:#20293d; }
'''


def configure_application(app):
    # Windows offscreen/RDP plugin may not enumerate system fonts automatically.
    # Add the existing local font rather than shipping or downloading a new font.
    if 'Segoe UI' not in QFontDatabase.families():
        for name in ('segoeui.ttf','seguisb.ttf','segoeuib.ttf','seguisym.ttf'):
            path = Path('C:/Windows/Fonts') / name
            if path.is_file(): QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont('Segoe UI',10)); app.setStyle('Fusion'); app.setStyleSheet(THEME)


def button(text, slot, primary=False):
    widget = QPushButton(text); widget.setProperty('primary', primary)
    widget.clicked.connect(lambda checked=False: slot())
    return widget


class Value:
    def __init__(self, widget=None, value=None):
        self.widget, self.value = widget, value

    def get(self):
        w = self.widget
        if isinstance(w, QCheckBox): return w.isChecked()
        if isinstance(w, QComboBox): return w.currentData() if w.currentData() is not None else w.currentText()
        if isinstance(w, QSpinBox): return w.value()
        if isinstance(w, (QLineEdit, QLabel)): return w.text()
        return self.value

    def set(self, value):
        self.value = value; w = self.widget
        if isinstance(w, QCheckBox): w.setChecked(bool(value))
        elif isinstance(w, QComboBox):
            index = w.findData(value)
            if index < 0: index = w.findText(str(value))
            if index >= 0: w.setCurrentIndex(index)
        elif isinstance(w, QSpinBox): w.setValue(int(value))
        elif w is not None: w.setText(str(value))


class Text:
    def __init__(self, widget): self.widget = widget
    def get(self, *_): return self.widget.toPlainText()
    def delete(self, *_): self.widget.clear()
    def insert(self, position, value):
        cursor = self.widget.textCursor(); cursor.movePosition(QTextCursor.End if position == 'end' else QTextCursor.Start)
        cursor.insertText(str(value)); self.widget.setTextCursor(cursor)
    def configure(self, **kw):
        if 'state' in kw: self.widget.setReadOnly(kw['state'] == 'disabled')
    def see(self, *_): self.widget.verticalScrollBar().setValue(self.widget.verticalScrollBar().maximum())


class Control:
    def __init__(self, widget): self.widget = widget
    def configure(self, **kw):
        if 'state' in kw: self.widget.setEnabled(kw['state'] != 'disabled')


class Table:
    def __init__(self, labels, tree_title=None):
        self.widget = QTreeWidget(); self.widget.setRootIsDecorated(False)
        self.widget.setAlternatingRowColors(True); self.widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.labels, self.tree_title, self.nodes, self.sequence = labels, tree_title, {}, 0
        self.widget.setHeaderLabels(([tree_title] if tree_title else []) + labels)
        self.widget.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.widget.header().setStretchLastSection(True)
    def insert(self, parent='', where='end', iid=None, values=(), text=''):
        self.sequence += 1; iid = str(iid or f'row_{self.sequence}')
        item = QTreeWidgetItem(([str(text)] if self.tree_title else []) + [str(x) for x in values])
        item.setData(0, Qt.UserRole, (iid, tuple(values))); self.widget.addTopLevelItem(item); self.nodes[iid] = item
        return iid
    def get_children(self): return list(self.nodes)
    def delete(self, *ids):
        for iid in ids:
            item = self.nodes.pop(str(iid), None)
            if item is not None: self.widget.takeTopLevelItem(self.widget.indexOfTopLevelItem(item))
    def selection(self): return [item.data(0, Qt.UserRole)[0] for item in self.widget.selectedItems()]
    def selection_set(self, iid):
        if str(iid) in self.nodes: self.widget.setCurrentItem(self.nodes[str(iid)])
    def item(self, iid, option=None, **kwargs):
        item = self.nodes[str(iid)]; _, values = item.data(0, Qt.UserRole)
        if 'values' in kwargs:
            values = tuple(kwargs['values']); item.setData(0, Qt.UserRole, (str(iid), values))
            offset = 1 if self.tree_title else 0
            for index, value in enumerate(values): item.setText(index + offset, str(value))
        return values if option == 'values' else {'values': values}


def placeholder(text='VIDEO'):
    pixmap = QPixmap(220, 132); pixmap.fill(QColor('#18263e'))
    painter = QPainter(pixmap); painter.setPen(QColor('#9ed5ff'))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, text); painter.end(); return QIcon(pixmap)


class CardDelegate(QStyledItemDelegate):
    def paint(self,painter,option,index):
        painter.fillRect(option.rect,QColor('#0e1421'))


class Gallery:
    def __init__(self, media_dir, thumbnails, prompt_callback=None):
        self.widget = QListWidget(); self.widget.setViewMode(QListWidget.IconMode)
        self.widget.setIconSize(QSize(200, 120)); self.widget.setGridSize(QSize(222, 182))
        self.widget.setResizeMode(QListWidget.Adjust); self.widget.setWordWrap(True)
        self.widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.prompt_callback=prompt_callback; self.cards={}
        if prompt_callback:
            self.widget.setGridSize(QSize(250,220)); self.widget.setItemDelegate(CardDelegate(self.widget))
        self.nodes, self.media_dir, self.thumbnails = {}, Path(media_dir), thumbnails
        thumbnails.ready.connect(self.icon_ready)
    def insert(self, parent='', where='end', iid=None, values=(), text=''):
        iid = str(iid); item = QListWidgetItem(placeholder('VIDEO' if values[0]=='video' else 'ẢNH'), f'{values[-1]}\n{values[0]} · {values[1]}')
        item.setToolTip(str(values[-1])); item.setData(Qt.UserRole, (iid, tuple(values)))
        self.widget.addItem(item); self.nodes[iid] = item
        if self.prompt_callback:
            card=QWidget(); card.setAutoFillBackground(True); card.setStyleSheet('background:#0e1421;'); layout=QVBoxLayout(card); layout.setContentsMargins(8,8,8,8); layout.setSpacing(6)
            item.setSizeHint(QSize(246,214))
            preview=QLabel(); preview.setFixedHeight(120); preview.setAlignment(Qt.AlignCenter); preview.setPixmap(item.icon().pixmap(200,120)); layout.addWidget(preview)
            row=QHBoxLayout(); title=QLabel(str(values[-1])); title.setWordWrap(True); row.addWidget(title,1)
            prompt_button=button('Prompt',lambda:self.prompt_callback(iid)); prompt_button.setFixedWidth(76); prompt_button.setStyleSheet('padding:5px 6px;background:#18263e;'); row.addWidget(prompt_button); layout.addLayout(row)
            item.setText(' '); item.setIcon(QIcon()); self.widget.setItemWidget(item,card); self.cards[iid]=preview
        self.thumbnails.request(self.media_dir / str(values[-1])); return iid
    def get_children(self): return list(self.nodes)
    def delete(self, *ids):
        for iid in ids:
            self.cards.pop(str(iid),None)
            item = self.nodes.pop(str(iid), None)
            if item is not None: self.widget.takeItem(self.widget.row(item))
    def selection(self): return [x.data(Qt.UserRole)[0] for x in self.widget.selectedItems()]
    def selection_set(self, iid):
        if str(iid) in self.nodes: self.widget.setCurrentItem(self.nodes[str(iid)])
    def item(self, iid, option=None):
        values = self.nodes[str(iid)].data(Qt.UserRole)[1]
        return values if option == 'values' else {'values': values}
    def icon_ready(self, path, image):
        name = Path(path).name
        if name in self.nodes and not self.prompt_callback: self.nodes[name].setIcon(QIcon(QPixmap.fromImage(image)))
        if name in self.cards:self.cards[name].setPixmap(QPixmap.fromImage(image).scaled(200,120,Qt.KeepAspectRatio,Qt.SmoothTransformation))


class References(QGroupBox):
    def __init__(self):
        super().__init__('Tham chiếu — tối đa 8 ảnh + 8 tài liệu; 5 MB/tệp')
        self.items = []
        self.roles = {'Tham khảo': 'reference', 'Nhân vật': 'character', 'Bối cảnh': 'scene', 'Nền': 'background', 'Khung hình đầu': 'first_frame', 'Tài liệu': 'document'}
        layout = QVBoxLayout(self)
        self.list = QListWidget(); self.list.setViewMode(QListWidget.IconMode)
        self.list.setIconSize(QSize(130, 82)); self.list.setGridSize(QSize(160, 126))
        self.list.setFlow(QListWidget.LeftToRight); self.list.setWrapping(False)
        self.list.setFixedHeight(145); layout.addWidget(self.list)
        row = QHBoxLayout(); layout.addLayout(row)
        row.addWidget(button('Thêm ảnh / tài liệu…', self.add)); row.addWidget(button('Bỏ tệp chọn', self.remove)); row.addWidget(button('Bỏ tất cả', self.clear))
        self.role_combo = QComboBox(); self.role_combo.addItems(list(self.roles)); row.addWidget(self.role_combo); row.addStretch()
        self.role_combo.activated.connect(self.change_role)
        self.list.currentRowChanged.connect(self.selected)
    def add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Chọn ảnh hoặc tài liệu', '', 'Tham chiếu (*.png *.jpg *.jpeg *.webp *.md *.txt *.csv *.xlsx *.xls *.pdf *.docx)')
        if not paths: return
        try:
            if len(self.items) + len(paths) > 16: raise ValueError('Tối đa 8 ảnh và 8 tài liệu')
            additions = []
            for raw in paths:
                path = Path(raw)
                if path.stat().st_size > 5 * 1024 * 1024: raise ValueError('Mỗi ảnh tối đa 5 MB')
                data = 'data:' + str(mimetypes.guess_type(path.name)[0]) + ';base64,' + base64.b64encode(path.read_bytes()).decode()
                additions.append({'name': path.name, 'data': data, 'role': 'reference' if path.suffix.lower() in ('.png','.jpg','.jpeg','.webp') else 'document'})
            items = self.items + additions
            from reference_files import validate_files
            images=[x for x in items if x['role']!='document'];documents=[x for x in items if x['role']=='document']
            validate_references([x['data'] for x in images], [x['role'] for x in images])
            validate_files([{'name':x['name'],'data':x['data']} for x in documents])
            self.items = items; self.render()
        except (ValueError, OSError) as exc: QMessageBox.warning(self, 'Ảnh mẫu', str(exc))
    def render(self):
        self.list.clear()
        for index, item in enumerate(self.items):
            pixmap = QPixmap(); pixmap.loadFromData(base64.b64decode(item['data'].split(',', 1)[1]))
            icon = QIcon(pixmap) if not pixmap.isNull() else placeholder('TỆP')
            name=item['name'] if len(item['name'])<=18 else item['name'][:16]+'…'
            row = QListWidgetItem(icon, f"{index+1}. {name}"); row.setToolTip(item['name']); self.list.addItem(row)
        if self.items: self.list.setCurrentRow(0)
    def selected(self, index):
        if 0 <= index < len(self.items):
            name = next(k for k,v in self.roles.items() if v == self.items[index]['role']); self.role_combo.setCurrentText(name)
    def change_role(self, *_):
        index = self.list.currentRow()
        if 0 <= index < len(self.items) and self.items[index]['role']!='document' and self.role_combo.currentText()!='Tài liệu': self.items[index]['role'] = self.roles[self.role_combo.currentText()]
    def remove(self):
        index = self.list.currentRow()
        if 0 <= index < len(self.items): self.items.pop(index); self.render()
    def clear(self): self.items = []; self.render()
    def payload(self): return {'reference_images':[x['data'] for x in self.items if x['role']!='document'], 'reference_roles':[x['role'] for x in self.items if x['role']!='document'], 'reference_files':[{'name':x['name'],'data':x['data']} for x in self.items if x['role']=='document']}


class Root:
    def __init__(self, window): self.window = window
    def after(self, ms, callback): QTimer.singleShot(ms, callback)
    def clipboard_clear(self): QApplication.clipboard().clear()
    def clipboard_append(self, text): QApplication.clipboard().setText(text)


class Dialogs:
    @staticmethod
    def parent(kwargs):
        p = kwargs.get('parent'); return p.window if isinstance(p, Root) else (p if isinstance(p, QWidget) else None)
    @staticmethod
    def showerror(title, text, **kw): QMessageBox.critical(Dialogs.parent(kw), title, text)
    @staticmethod
    def showinfo(title, text, **kw): QMessageBox.information(Dialogs.parent(kw), title, text)
    @staticmethod
    def showwarning(title, text, **kw): QMessageBox.warning(Dialogs.parent(kw), title, text)
    @staticmethod
    def askyesno(title, text, **kw): return QMessageBox.question(Dialogs.parent(kw), title, text) == QMessageBox.Yes
    @staticmethod
    def askstring(title, prompt, **kw):
        value, ok = QInputDialog.getText(Dialogs.parent(kw), title, prompt, text=kw.get('initialvalue', '')); return value if ok else None
    @staticmethod
    def askopenfilename(**kw):
        filt = ';;'.join(f'{label} ({pattern})' for label,pattern in kw.get('filetypes', []))
        return QFileDialog.getOpenFileName(Dialogs.parent(kw), kw.get('title', 'Chọn tệp'), '', filt)[0]
    @staticmethod
    def asksaveasfilename(**kw): return QFileDialog.getSaveFileName(Dialogs.parent(kw), 'Lưu tệp kết quả', kw.get('initialfile', ''))[0]
    @staticmethod
    def askdirectory(**kw): return QFileDialog.getExistingDirectory(Dialogs.parent(kw), 'Chọn thư mục', kw.get('initialdir', ''))


class StudioWindow(QMainWindow, core.DesktopStudio):
    def __init__(self, workspace=None, api=None, auto_connect=True):
        QMainWindow.__init__(self)
        configure_application(QApplication.instance())
        if workspace is not None: core.ROOT = Path(workspace)
        core.messagebox = core.simpledialog = core.filedialog = Dialogs
        self.root = Root(self)
        core.configure(core.ROOT); self.env = core.local_environment(core.ROOT)
        self.api = api or core.LocalAPI(self.env)
        self.process, self.log_handle, self.ready = None, None, False
        self.events = queue.Queue(); self.closing = self.checking = self.chat_busy = False
        self.service_log_offset = 0; self.active_media, self.active_batches = {}, {}
        self.account_selectors=[]
        self.sequence_accounts={}
        try:self.sequence_accounts=json.loads((core.ROOT/'data/desktop_sequence_accounts.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        self.chat_accounts={}
        try:self.chat_accounts=json.loads((core.ROOT/'data/desktop_chat_accounts.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        self.current_chat_account='auto'
        self.telegram_dirty = self.telegram_saving = False
        self.history_path = core.ROOT / 'data' / 'desktop_chat.json'
        try: self.messages = json.loads(self.history_path.read_text('utf-8')) if self.history_path.exists() else []
        except (ValueError, OSError): self.messages = []
        self.setWindowTitle('Muse2API 1.6.9 · Studio desktop'); self.resize(1280, 900); self.setMinimumSize(1020, 760)
        screen=QApplication.primaryScreen()
        if screen and QApplication.platformName()!='offscreen':
            area=screen.availableGeometry()
            width,height=max(700,area.width()-32),max(600,area.height()-64)
            self.setMinimumSize(min(1020,width),min(760,height))
            self.resize(min(1280,width),min(900,height))
        body = QWidget(); layout = QVBoxLayout(body); layout.setContentsMargins(18, 14, 18, 14); layout.setSpacing(12)
        self.setCentralWidget(body)
        row = QHBoxLayout(); layout.addLayout(row)
        heading = QLabel('● Muse2API Studio'); heading.setObjectName('heading'); row.addWidget(heading)
        row.addWidget(button('Chạy dịch vụ', self.start_service, True)); row.addWidget(button('Dừng', self.stop_service))
        row.addWidget(button('Khởi động lại', self.restart_service)); row.addWidget(button('Mở Web', lambda: core.webbrowser.open(self.api.base+'/admin')))
        row.addStretch(); status = QLabel('Chưa kết nối dịch vụ'); status.setObjectName('muted'); self.status = Value(status); row.addWidget(status)
        self.version_notice = QLabel(''); self.version_notice.setObjectName('warning'); self.version_notice.setWordWrap(True); layout.addWidget(self.version_notice)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.thumbnails = ThumbnailQueue(core.ROOT / 'data' / 'thumbnails', self)
        self.chat_tab = self.page('Chat Muse'); self.media_tab = self.page('Tạo ảnh / video'); self.batch_tab = self.page('Excel / TXT')
        self.library_tab = self.page('Thư viện'); self.accounts_tab = self.page('Tài khoản'); self.settings_tab = self.page('Cài đặt'); self.log_tab = self.page('Nhật ký')
        self.build_chat(); self.build_media(); self.build_batch(); self.build_library(); self.build_accounts(); self.build_settings()
        row=QHBoxLayout();self.log_tab.layout().addLayout(row)
        row.addWidget(QLabel('Tài khoản'));self.log_account=self.account_picker(row,filtering=True)
        self.log_account.widget.currentIndexChanged.connect(lambda _:self.refresh_library())
        row.addWidget(button('Làm mới tác vụ',self.refresh_library));row.addWidget(button('Tạo lại cùng tài khoản',lambda:self.retry_selected_task(self.log_tasks)))
        row.addWidget(button('Tạo riêng bằng tài khoản khác',lambda:self.retry_selected_task(self.log_tasks,True)))
        self.log_tasks=Table(['Tài khoản','Loại','Trạng thái','Prompt / lỗi']);self.log_tab.layout().addWidget(self.log_tasks.widget,1)
        log = QPlainTextEdit();log.setMaximumHeight(170); log.setReadOnly(True); self.log_tab.layout().addWidget(log); self.log_text = Text(log)
        self.tabs.currentChanged.connect(lambda _: (self.media_viewer.pause(), self.library_viewer.pause(), self.chat_viewer.pause(), self.refresh_library() if self.ready and self.tabs.currentWidget() in (self.library_tab,self.log_tab) else None))
        self.task_refresh_timer=QTimer(self);self.task_refresh_timer.timeout.connect(lambda:self.refresh_library() if self.ready and self.tabs.currentWidget() in (self.library_tab,self.log_tab) else None);self.task_refresh_timer.start(3000)
        QTimer.singleShot(100, self.drain)
        if auto_connect: QTimer.singleShot(500, self.refresh_status)

    def account_picker(self, layout, filtering=False):
        combo=QComboBox();combo.addItem('Tất cả tài khoản' if filtering else 'Tự động chọn','')
        if filtering:combo.addItem('Chưa phân loại','unassigned')
        layout.addWidget(combo);self.account_selectors.append((combo,filtering))
        return Value(combo)

    def update_account_choices(self, accounts):
        for combo,filtering in self.account_selectors:
            old=combo.currentData();combo.blockSignals(True);combo.clear()
            combo.addItem('Tất cả tài khoản' if filtering else 'Tự động chọn','')
            if filtering:combo.addItem('Chưa phân loại','unassigned')
            for a in accounts:
                if filtering or a.get('enabled',True):combo.addItem(a['label']+' · '+a['id'],a['id'])
            if old and combo.findData(old)<0:combo.addItem(old+' (không khả dụng)',old)
            combo.setCurrentIndex(max(0,combo.findData(old or '')));combo.blockSignals(False)

    def save_account_chat(self):
        self.chat_accounts[self.current_chat_account]={'id':getattr(self,'chat_session_id',None),'messages':self.messages[-100:]}
        core.atomic_json(core.ROOT/'data/desktop_chat_accounts.json',self.chat_accounts)

    def switch_account_chat(self, *_):
        if self.chat_busy:return
        self.save_account_chat();self.current_chat_account=self.chat_account.get() or 'auto'
        state=self.chat_accounts.get(self.current_chat_account,{})
        self.chat_session_id=state.get('id') or __import__('uuid').uuid4().hex
        self.messages=list(state.get('messages',[]));self.render_chat()
        core.atomic_json(core.ROOT/'data/desktop_chat_session.json',{'id':self.chat_session_id})
        self.save_account_chat()

    def choose_account(self):
        from PySide6.QtWidgets import QInputDialog
        choices={a['label']+' · '+aid:aid for aid,a in self.account_data.items() if a.get('enabled',True)}
        if not choices:self.error('Thêm tài khoản khả dụng trước');return None
        text,ok=QInputDialog.getItem(self,'Tạo riêng bằng tài khoản khác','Tài khoản đích (tạo độc lập, hội thoại mới):',list(choices),0,False)
        return choices.get(text) if ok else None

    def clone_failed_batch(self):
        selected=self.batch_results.selection()
        if not selected:return self.error('Chọn lô có prompt lỗi trước')
        account=self.choose_account()
        if account:self.dispatch(lambda:self.api.call('/v1/batches/'+selected[0]+'/clone-failed?account_id='+account,{}),lambda _:self.refresh_batches())

    def open_media_folder(self):
        folder=core.ROOT/'data/media';account=self.library_account.get()
        if account and account!='unassigned':
            from account_media import account_folder
            folder=account_folder(folder,account)
        folder.mkdir(parents=True,exist_ok=True)
        core.os.startfile(str(folder)) if core.os.name=='nt' else core.webbrowser.open(folder.as_uri())

    def page(self, title):
        widget = QWidget(); layout = QVBoxLayout(widget); layout.setContentsMargins(14, 14, 14, 14); layout.setSpacing(10)
        self.tabs.addTab(widget, title); return widget

    def build_chat(self):
        layout = self.chat_tab.layout();row=QHBoxLayout();layout.addLayout(row);row.addWidget(QLabel('Tài khoản chat'));self.chat_account=self.account_picker(row);self.chat_account.widget.currentIndexChanged.connect(self.switch_account_chat)
        split=QSplitter(); self.chat_split=split; layout.addWidget(split,1)
        transcript = QTextBrowser(); self.transcript = Text(transcript); split.addWidget(transcript)
        self.chat_media_panel=QWidget(); media_layout=QVBoxLayout(self.chat_media_panel); split.addWidget(self.chat_media_panel)
        self.chat_gallery=Gallery(core.ROOT/'data'/'media',self.thumbnails); self.chat_gallery.widget.setMaximumHeight(190)
        media_layout.addWidget(self.chat_gallery.widget)
        self.chat_viewer=MediaViewer(); media_layout.addWidget(self.chat_viewer,1)
        self.chat_gallery.widget.itemSelectionChanged.connect(lambda:self.open_result(self.chat_gallery,quiet=True))
        self.chat_gallery.widget.itemDoubleClicked.connect(lambda _:self.open_result(self.chat_gallery,autoplay=True))
        media_layout.addWidget(button('Lưu tệp…',lambda:self.save_result(self.chat_gallery)))
        self.chat_media_panel.hide(); split.setSizes([760,420])
        entry = QPlainTextEdit(); entry.setPlaceholderText('Nhập tin nhắn cho Muse…'); entry.setMaximumHeight(115); self.chat_input = Text(entry); layout.addWidget(entry)
        row = QHBoxLayout(); layout.addLayout(row)
        send = button('Gửi · Ctrl+Enter', self.send_chat, True); self.chat_button = Control(send); row.addWidget(send)
        row.addWidget(button('Chat mới', self.new_chat)); label = QLabel(); self.chat_status = Value(label); row.addWidget(label); row.addStretch()
        QShortcut(QKeySequence('Ctrl+Return'), entry, activated=self.send_chat)
        self.render_chat()

    def render_chat(self):
        text = ''.join('<div style="padding:12px;background:#121a2b;margin-bottom:12px"><b style="color:#9ed5ff">'+
                       ('Bạn' if msg['role']=='user' else 'Muse')+'</b><p style="white-space:pre-wrap">'+html.escape(msg['content']).replace('\n','<br>')+'</p></div>' for msg in self.messages)
        self.transcript.widget.setHtml(text); self.transcript.see('end')
        self.chat_gallery.delete(*self.chat_gallery.get_children())
        for msg in self.messages:
            for media in msg.get('media',[]):
                name=media.get('filename','')
                if name and name not in self.chat_gallery.nodes:
                    self.chat_gallery.insert(iid=name,values=(media.get('kind','image'),str(media.get('size',0))+' bytes',name))
        was_visible = self.chat_media_panel.isVisible()
        self.chat_media_panel.setVisible(bool(self.chat_gallery.nodes))
        if self.chat_gallery.nodes and not was_visible: self.chat_split.setSizes([760,420])

    def show_chat_media(self, items):
        if items:
            self.chat_gallery.selection_set(items[0]['filename']); self.open_result(self.chat_gallery,quiet=True)
            self.refresh_library()

    def media_controls(self, layout):
        row = QHBoxLayout(); layout.addLayout(row)
        kind = QComboBox(); kind.addItem('Tạo ảnh', 'image'); kind.addItem('Tạo video', 'video'); kind.setCurrentIndex(1)
        size = QComboBox(); size.addItems(['1280x720','720x1280','1024x1024','1080x1920'])
        duration = QSpinBox(); duration.setRange(1,10); duration.setValue(10)
        row.addWidget(QLabel('Loại')); row.addWidget(kind); row.addWidget(QLabel('Kích thước')); row.addWidget(size)
        row.addWidget(QLabel('Giây video')); row.addWidget(duration); row.addStretch()
        kind.currentIndexChanged.connect(lambda _: duration.setEnabled(kind.currentData()=='video'))
        return Value(kind), Value(size), Value(duration)

    def generation_controls(self, layout, prefix):
        box=QGroupBox('Cơ chế tạo cảnh'); form=QVBoxLayout(box); layout.addWidget(box)
        row=QHBoxLayout(); form.addLayout(row)
        mode=QComboBox(); mode.addItem('Tạo độc lập','independent'); mode.addItem('Chuỗi đồng bộ nhân vật','coherent')
        lang=QComboBox(); lang.addItem('Ngôn ngữ: Tự động','auto'); lang.addItem('Tiếng Việt','vi'); lang.addItem('English','en')
        row.addWidget(mode,1); row.addWidget(lang)
        if prefix=='media':row.addWidget(button('Chuỗi mới',self.new_generation_sequence))
        hint=QLabel('Độc lập: mỗi prompt một hội thoại và gửi kèm ảnh mẫu. Đồng bộ: cùng hội thoại/tài khoản, tệp gửi một lần; quy ước chung gửi mỗi cảnh.'); hint.setWordWrap(True); form.addWidget(hint)
        notes=QPlainTextEdit(); notes.setMaximumHeight(60); notes.setPlaceholderText('Quy ước chung: nhân vật, trang phục, đạo cụ, phong cách… (dùng cho chuỗi đồng bộ)'); form.addWidget(notes)
        notes.setEnabled(False); mode.currentIndexChanged.connect(lambda _:notes.setEnabled(mode.currentData()=='coherent'))
        setattr(self,prefix+'_generation_mode',Value(mode)); setattr(self,prefix+'_prompt_language',Value(lang)); setattr(self,prefix+'_continuity_notes',Text(notes))

    def build_media(self):
        layout = self.media_tab.layout(); split = QSplitter(); layout.addWidget(split, 1)
        form = QWidget(); form_layout = QVBoxLayout(form); form_layout.setContentsMargins(0,0,10,0); scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(form); split.addWidget(scroll)
        row=QHBoxLayout();form_layout.addLayout(row);row.addWidget(QLabel('Tài khoản tạo'));self.media_account=self.account_picker(row)
        self.kind,self.size,self.duration = self.media_controls(form_layout)
        self.generation_controls(form_layout,'media')
        prompt = QPlainTextEdit(); prompt.setPlaceholderText('Mô tả ảnh/video và cách dùng từng ảnh mẫu…'); prompt.setMaximumHeight(115)
        self.prompt = Text(prompt); form_layout.addWidget(prompt)
        self.references = References(); form_layout.addWidget(self.references)
        row = QHBoxLayout(); form_layout.addLayout(row)
        send = button('Tạo ảnh / video', self.create_media, True); self.media_button = Control(send); row.addWidget(send)
        label = QLabel(); label.setWordWrap(True); self.media_status = Value(label); row.addWidget(label,1)
        self.media_results = Table(['Loại','Trạng thái','Tệp kết quả']); form_layout.addWidget(self.media_results.widget,1)
        self.media_results.widget.itemSelectionChanged.connect(lambda: self.open_result(self.media_results, quiet=True))
        row = QHBoxLayout(); form_layout.addLayout(row)
        row.addWidget(button('Xem trong GUI', lambda:self.open_result(self.media_results)))
        row.addWidget(button('Lưu kết quả…', lambda:self.save_result(self.media_results))); row.addStretch()
        self.media_viewer = MediaViewer(); split.addWidget(self.media_viewer); split.setSizes([700,500])

    def build_batch(self):
        outer=self.batch_tab.layout(); scroll=QScrollArea(); scroll.setWidgetResizable(True); content=QWidget(); layout=QVBoxLayout(content); scroll.setWidget(content); outer.addWidget(scroll)
        row=QHBoxLayout();layout.addLayout(row);row.addWidget(QLabel('Tài khoản chạy danh sách'));self.batch_account=self.account_picker(row)
        layout.addWidget(QLabel('Mỗi tài khoản chạy một danh sách lần lượt. Chọn tài khoản khác và gửi lô mới để chạy song song.'))
        self.batch_kind,self.batch_size,self.batch_duration = self.media_controls(layout)
        self.generation_controls(layout,'batch')
        row = QHBoxLayout(); layout.addLayout(row); row.addWidget(button('Nhập Excel / TXT…', self.import_prompts))
        combo = QComboBox(); combo.addItems(['Mỗi dòng một prompt','Ngăn cách bằng dòng trống']); self.batch_split=Value(combo); row.addWidget(combo)
        label=QLabel('Tối đa 500 prompt'); self.batch_count=Value(label); row.addWidget(label); row.addStretch()
        prompts=QPlainTextEdit(); prompts.setPlaceholderText('Nhập danh sách prompt hoặc chọn file Excel / TXT'); prompts.setMaximumHeight(130); self.batch_prompts=Text(prompts); layout.addWidget(prompts)
        self.batch_refs=References(); layout.addWidget(self.batch_refs)
        layout.addWidget(QLabel('Đồng bộ: gửi ảnh/tài liệu một lần ở đầu hội thoại. Độc lập: gửi lại cho từng prompt.'))
        row=QHBoxLayout(); layout.addLayout(row)
        send=button('Chạy danh sách',self.create_batch,True); self.batch_button=Control(send); row.addWidget(send)
        for action,label in [('pause','Tạm dừng'),('resume','Tiếp tục'),('stop','Dừng prompt chờ')]: row.addWidget(button(label,lambda action=action:self.control_batch(action)))
        row.addWidget(button('Tạo lại prompt lỗi',lambda:self.control_batch('retry-failed')))
        row.addWidget(button('Lỗi → tài khoản khác',self.clone_failed_batch))
        row.addWidget(button('Xuất báo cáo…',lambda:self.export_report(True)))
        row.addWidget(button('Làm mới',self.refresh_batches)); row.addStretch()
        self.batch_results=Table(['Tài khoản / Loại','Tiến độ','Tệp mẫu','Tạm dừng'],'Mã lô'); layout.addWidget(self.batch_results.widget,1)
        self.batches={}; label=QLabel(); label.setWordWrap(True); self.batch_details=Value(label); layout.addWidget(label)
        self.batch_results.widget.itemSelectionChanged.connect(self.show_batch_details)

    def build_library(self):
        layout=self.library_tab.layout(); row=QHBoxLayout(); layout.addLayout(row)
        row.addWidget(QLabel('Thư mục tài khoản'));self.library_account=self.account_picker(row,filtering=True);self.library_account.widget.currentIndexChanged.connect(lambda _:self.refresh_library())
        row.addWidget(button('Làm mới thư viện',self.refresh_library,True))
        row.addWidget(button('Xem trong GUI',lambda:self.open_result(self.library)))
        row.addWidget(button('Lưu tệp…',lambda:self.save_result(self.library)))
        row.addWidget(button('Mở thư mục',self.open_media_folder)); row.addStretch()
        split=QSplitter(); layout.addWidget(split,1)
        self.library_metadata={}
        self.library=Gallery(core.ROOT/'data'/'media',self.thumbnails,self.show_library_prompt); split.addWidget(self.library.widget)
        self.library_viewer=MediaViewer(); split.addWidget(self.library_viewer); split.setSizes([710,490])
        self.library.widget.itemSelectionChanged.connect(lambda:self.open_result(self.library,quiet=True))
        self.library.widget.itemDoubleClicked.connect(lambda _:self.open_result(self.library,autoplay=True))
        self.task_list=Table(['Tài khoản','Loại','Trạng thái','Prompt']); self.task_list.widget.setMaximumHeight(145)
        row=QHBoxLayout(); row.addWidget(QLabel('Tác vụ gần đây')); row.addStretch()
        row.addWidget(button('Tạo lại tác vụ lỗi',self.retry_selected_task));row.addWidget(button('Tạo riêng / tài khoản khác',lambda:self.retry_selected_task(other=True))); row.addWidget(button('Xuất báo cáo…',lambda:self.export_report(False)))
        layout.addLayout(row); layout.addWidget(self.task_list.widget)

    def refresh_library(self):
        # Keep gallery selection/player stable while an async task completes.
        selected=self.library.selection()
        def success(data):
            media,tasks=data
            media['media']=[item for item in media['media'] if not item['name'].startswith('.') and
                            Path(item['name']).suffix.lower() in VIDEO_EXTENSIONS|{'.png','.jpg','.jpeg','.webp','.gif','.bmp','.tif','.tiff','.svg'}]
            chosen=self.library_account.get()
            if chosen:media['media']=[item for item in media['media'] if (item.get('account_id') or 'unassigned')==chosen]
            self.library_metadata={item['name']:item for item in media['media']}
            names={item['name'] for item in media['media']}
            self.library.delete(*(x for x in self.library.get_children() if x not in names))
            for item in media['media']:
                if item['name'] not in self.library.nodes:
                    self.library.insert(iid=item['name'],values=(item['kind'],f"{item['bytes']/1048576:.2f} MB",item['name']))
            if selected and selected[0] in self.library.nodes: self.library.selection_set(selected[0])
            self.task_list.delete(*self.task_list.get_children())
            self.log_tasks.delete(*self.log_tasks.get_children())
            for task in tasks['tasks']:
                account=task.get('account') or 'unassigned';label=self.account_data.get(account,{}).get('label',account)
                if not chosen or chosen==account:self.task_list.insert(iid=task['id'],values=(label,task['kind'],task['status'],task['prompt'][:100]))
                if not self.log_account.get() or self.log_account.get()==account:self.log_tasks.insert(iid=task['id'],values=(label,task['kind'],task['status'],task.get('error') or task['prompt'][:160]))
        self.dispatch(lambda:(self.api.call('/admin/media'),self.api.call('/admin/tasks')),success)

    def show_library_prompt(self, name):
        item=self.library_metadata.get(name,{})
        dialog=QDialog(self); dialog.setWindowTitle(name+' · Prompt'); dialog.resize(720,510); layout=QVBoxLayout(dialog)
        layout.addWidget(QLabel('Prompt gốc — có thể chỉnh sửa trước khi nạp vào mục Tạo ảnh / video'))
        text=QPlainTextEdit(item.get('prompt') or ''); layout.addWidget(text)
        if item.get('api_prompt'):
            layout.addWidget(QLabel('Nội dung thực tế gửi Muse (gồm quy ước chung)'))
            actual=QPlainTextEdit(item['api_prompt']); actual.setReadOnly(True); layout.addWidget(actual)
        row=QHBoxLayout(); layout.addLayout(row)
        def edit():
            prompt=text.toPlainText().strip()
            if not prompt:return
            tid=item.get('task_id')
            if tid:
                self.dispatch(lambda:self.api.call('/v1/task-tools/'+tid+'/request'),lambda data:self.load_generation_request(data,prompt))
            else:
                self.prompt.widget.setPlainText(prompt); self.tabs.setCurrentWidget(self.media_tab)
            dialog.accept()
        row.addWidget(button('Sửa / tạo lại…',edit,True)); row.addWidget(button('Sao chép prompt',lambda:QApplication.clipboard().setText(text.toPlainText())))
        row.addWidget(button('Đóng',dialog.accept)); dialog.exec()

    def load_generation_request(self, data, prompt):
        r=data['request']; self.media_account.set(r.get('account_id') or ''); self.kind.set(data['kind']); size=r.get('aspect_ratio') or r.get('size') or '1280x720'
        if self.size.widget.findText(size)<0:self.size.widget.addItem(size)
        self.size.set(size); self.duration.set(r.get('duration') or 10)
        self.prompt.widget.setPlainText(prompt)
        self.media_generation_mode.set(r.get('generation_mode','independent')); self.media_prompt_language.set(r.get('prompt_language','auto'))
        self.media_continuity_notes.widget.setPlainText(r.get('continuity_notes',''))
        if r.get('sequence_id'):
            self.generation_sequence_id=r['sequence_id'];self.sequence_accounts[r.get('account_id') or 'auto']=r['sequence_id']
        refs=r.get('reference_images') or []
        if not refs and isinstance(r.get('reference_image'),str):refs=[r['reference_image']]
        roles=r.get('reference_roles') or ['reference']*len(refs)
        self.references.items=[{'name':f'Ảnh {i+1}','data':ref,'role':roles[i]} for i,ref in enumerate(refs) if ref.startswith('data:')]
        self.references.items += [{'name':f['name'],'data':f['data'],'role':'document'} for f in r.get('reference_files',[])]
        self.references.render(); self.tabs.setCurrentWidget(self.media_tab)
        self.media_status.set('Đã nạp prompt và thông số đã lưu. Kiểm tra rồi bấm Tạo ảnh / video.')

    def retry_selected_task(self, tree=None, other=False):
        selected=(tree or self.task_list).selection()
        if not selected:return self.error('Chọn tác vụ lỗi trong danh sách trước')
        def done(result):
            self.media_results.insert(iid=result['id'],values=('video' if result.get('object')=='video.task' else 'image','queued',''))
            self.active_media[result['id']]=True; self.poll_media(result['id'],'video' if result.get('object')=='video.task' else 'image'); self.refresh_library()
        account=self.choose_account() if other else None
        if other and not account:return
        suffix='?account_id='+account if account else ''
        self.dispatch(lambda:self.api.call('/v1/task-tools/'+selected[0]+'/retry'+suffix,{}),done)

    def export_report(self, batch=False):
        endpoint='/v1/task-tools/report'; basename='tac_vu'
        if batch:
            selected=self.batch_results.selection()
            if not selected:return self.error('Chọn một lô trước khi xuất')
            endpoint='/v1/batches/'+selected[0]+'/report'; basename=selected[0]
        dialog=QDialog(self); dialog.setWindowTitle('Chọn trạng thái xuất báo cáo'); layout=QVBoxLayout(dialog)
        options=[]
        for value,label in [('completed','Đã hoàn thành'),('queued','Đang chờ'),('processing,running,submitting','Đang thực hiện'),('failed','Lỗi'),('interrupted','Gián đoạn'),('cancelled','Đã dừng')]:
            cb=QCheckBox(label);cb.setChecked(True);layout.addWidget(cb);options.append((value,cb))
        layout.addWidget(button('Chọn nơi lưu…',dialog.accept,True))
        if dialog.exec()!=QDialog.Accepted:return
        statuses=','.join(value for value,cb in options if cb.isChecked())
        if not statuses:return self.error('Chọn ít nhất một trạng thái')
        if not batch and self.library_account.get():statuses+='&account_id='+self.library_account.get()
        path,fmt=QFileDialog.getSaveFileName(self,'Xuất báo cáo',basename+'.csv','CSV (*.csv);;JSON (*.json);;TXT (*.txt)')
        if not path:return
        def save(data):
            rows=data['items']; target=Path(path)
            if target.suffix.lower()=='.json':target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            elif target.suffix.lower()=='.txt':target.write_text('\n\n'.join(f"{r.get('index') or r.get('id')} | {r['status']}\n{r['prompt']}\n{r.get('filename') or ''}\n{r.get('error') or ''}" for r in rows),encoding='utf-8-sig')
            else:
                fields=['index','id','task_id','account','account_id','kind','prompt','status','error','filename','url','attempts']
                with target.open('w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader()
                    for r in rows:writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in r.items()})
            self.log(f'Đã xuất {len(rows)} dòng: {target}')
        self.dispatch(lambda:self.api.call(endpoint+'?statuses='+statuses),save)

    def open_result(self, tree, quiet=False, autoplay=False):
        try:
            path=self.result_path(tree)
            viewer=self.chat_viewer if tree is self.chat_gallery else (self.library_viewer if tree is self.library else self.media_viewer)
            if viewer.path!=path or autoplay: viewer.load(path,autoplay)
        except (ValueError,OSError) as exc:
            if not quiet: self.error(str(exc))

    def poll_media(self, tid, kind):
        if self.closing: return
        endpoint=('/v1/videos/' if kind=='video' else '/v1/images/tasks/')+tid
        def success(task):
            state=task.get('status',''); filename=task.get('result',{}).get('filename','')
            self.media_results.item(tid,values=(kind,state,filename))
            if state in ('completed','failed'):
                self.active_media.pop(tid,None)
                if state=='completed':
                    self.media_results.selection_set(tid); self.open_result(self.media_results,quiet=True); self.refresh_library()
                else:
                    self.log(task.get('error') or 'Tạo media thất bại');self.refresh_library()
            else: self.root.after(2000,lambda:self.poll_media(tid,kind))
        def failure(text):
            self.media_status.set(text); self.root.after(4000,lambda:self.poll_media(tid,kind))
        self.dispatch(lambda:self.api.call(endpoint),success,failure)

    def build_accounts(self):
        layout=self.accounts_tab.layout(); self.accounts=Table(['Tên','Phiên','Bật','Ghi chú']); layout.addWidget(self.accounts.widget,1)
        row=QHBoxLayout(); layout.addLayout(row)
        row.addWidget(button('Làm mới',self.refresh_accounts)); row.addWidget(button('Kiểm tra',lambda:self.account_action('test')))
        row.addWidget(button('Làm mới phiên',lambda:self.account_action('keepalive')))
        row.addWidget(button('Bật / Tắt',self.toggle_account)); row.addWidget(button('Đổi tên',self.rename_account)); row.addStretch()
        layout.addWidget(QLabel('Thêm / cập nhật tài khoản — mỗi dòng “tên | Cookie”, hoặc dán một Cookie'))
        entry=QPlainTextEdit(); entry.setMaximumHeight(130); self.cookie_input=Text(entry); layout.addWidget(entry)
        layout.addWidget(button('Nhập Cookie',self.add_accounts,True),alignment=Qt.AlignLeft); self.account_data={}

    def build_settings(self):
        outer=self.settings_tab.layout(); scroll=QScrollArea(); scroll.setWidgetResizable(True); outer.addWidget(scroll)
        content=QWidget(); layout=QVBoxLayout(content); scroll.setWidget(content)
        downloads=QGroupBox('Tải tự động'); d=QVBoxLayout(downloads); layout.addWidget(downloads)
        enabled=QCheckBox('Tự lưu ảnh / video hoàn tất'); self.auto_download=Value(enabled); d.addWidget(enabled)
        row=QHBoxLayout(); d.addLayout(row); folder=QLineEdit(); self.download_folder=Value(folder); row.addWidget(folder,1)
        row.addWidget(button('Chọn thư mục…',self.choose_download)); row.addWidget(button('Lưu',self.save_downloads))
        group=QGroupBox('Bot Telegram — chỉ nhận lệnh từ User ID được cho phép'); tg=QVBoxLayout(group); layout.addWidget(group)
        enabled=QCheckBox('Bật bot cùng dịch vụ'); self.telegram_enabled=Value(enabled); tg.addWidget(enabled)
        enabled.clicked.connect(self.mark_telegram_dirty)
        tg.addWidget(QLabel('Bot token — để trống để giữ token đã lưu'))
        token=QLineEdit(); token.setEchoMode(QLineEdit.Password); self.telegram_token=Value(token); tg.addWidget(token)
        token.textEdited.connect(self.mark_telegram_dirty)
        tg.addWidget(QLabel('Telegram User ID — ngăn cách bằng dấu phẩy'))
        ids=QLineEdit(); self.telegram_ids=Value(ids); tg.addWidget(ids)
        ids.textEdited.connect(self.mark_telegram_dirty)
        original=QCheckBox('Giữ ảnh gốc để tải và đăng (gửi một tệp)'); original.setChecked(True); self.telegram_original=Value(original); tg.addWidget(original)
        original.clicked.connect(self.mark_telegram_dirty)
        row=QHBoxLayout(); tg.addLayout(row); row.addWidget(button('Lưu Telegram',self.save_telegram,True)); row.addWidget(button('Xem trạng thái',self.load_settings)); row.addStretch()
        status=QLabel(); status.setWordWrap(True); status.setObjectName('warning'); self.telegram_status=Value(status); tg.addWidget(status)
        hint=QLabel('/start → chọn Tạo ảnh / Tạo video → gửi prompt và ảnh mẫu → chọn vai trò → Tạo / Gửi.\n/role 1 character · /ratio 9:16 · /duration 10 · /remove 1 · /last\nMáy và dịch vụ cần đang chạy để bot hoạt động.'); hint.setWordWrap(True); hint.setObjectName('muted'); tg.addWidget(hint)
        layout.addWidget(button('Sao chép API key',self.copy_key),alignment=Qt.AlignLeft); layout.addStretch()
        self.show_telegram(core.TelegramSettings(core.ROOT/'data').public())

    def show_telegram(self, data):
        draft = None
        if self.telegram_dirty and not self.telegram_saving:
            draft = (self.telegram_token.get(),self.telegram_ids.get(),self.telegram_enabled.get(),self.telegram_original.get())
        core.DesktopStudio.show_telegram(self,data)
        if draft is not None:
            self.telegram_token.set(draft[0]); self.telegram_ids.set(draft[1]); self.telegram_enabled.set(draft[2]); self.telegram_original.set(draft[3])
        else:
            self.telegram_dirty=False
        self.telegram_saving=False
        self.version_notice.setText('Phiên dịch vụ đang chạy là bản cũ. Cấu hình Telegram được giữ trên máy; khởi động lại dịch vụ để dùng các chức năng mới.' if data.get('restart_required') else '')

    def mark_telegram_dirty(self, *_):
        self.telegram_dirty=True

    def save_telegram(self):
        self.telegram_saving=True
        core.DesktopStudio.save_telegram(self)

    def error(self, text):
        self.telegram_saving=False
        core.DesktopStudio.error(self,text)

    def connected(self, data):
        core.DesktopStudio.connected(self,data)
        if data.get('desktop_revision',0)<3:
            self.version_notice.setText('Dịch vụ đang chạy chưa cập nhật. Khởi động lại dịch vụ để đồng bộ với GUI mới.')

    def restart_service(self):
        if not self.process or self.process.poll() is not None:
            QMessageBox.information(self,'Khởi động lại','Phiên này được mở từ CMD hoặc GUI cũ. Đóng cửa sổ đó, sau đó bấm Chạy dịch vụ tại đây. Cấu hình Telegram đã lưu sẽ được giữ nguyên.'); return
        self.stop_service()
        if self.status.get() != 'Đang dừng dịch vụ…': return
        deadline = time.monotonic() + 20
        def wait():
            if time.monotonic() > deadline:
                self.status.set('Dịch vụ cũ chưa dừng xong. Bấm Chạy dịch vụ khi cổng đã được giải phóng.'); return
            ports = core.get_ports(self.env)
            if (self.process and self.process.poll() is None) or any(not core.port_is_free(p) for p in ports): QTimer.singleShot(200,wait)
            else: self.ready=False; self.start_service()
        if self.process.poll() is not None: wait()
        else: QTimer.singleShot(200,wait)

    def closeEvent(self,event):
        owned=self.process and self.process.poll() is None
        if not self.closing and owned and QMessageBox.question(self,'Đóng Studio','Đóng GUI sẽ dừng phiên dịch vụ và bot do GUI mở. Tác vụ đang chạy có thể bị gián đoạn. Đóng?')!=QMessageBox.Yes:
            event.ignore(); return
        self.closing=True; self.chat_viewer.shutdown(); self.media_viewer.shutdown(); self.library_viewer.shutdown(); self.thumbnails.shutdown()
        if owned:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except core.subprocess.TimeoutExpired: self.process.kill()
        if self.log_handle: self.log_handle.close()
        event.accept()
