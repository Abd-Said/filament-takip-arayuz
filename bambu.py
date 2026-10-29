import os
import shutil
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget, QAbstractItemView,
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stok.db")
SCAN_INTERVAL_MS = 3000   # klasör tarama aralığı
ARCHIVE_DIR = "islenenler"  # "taşı" seçeneğinde kullanılan alt klasör
LOW_STOCK_G = 150         # bunun altı kırmızı görünür
TYPES = ["PLA", "PLA+", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "PLA-CF", "PETG-CF"]


# ----------------------------------------------------------------------------
# 3mf okuma
# ----------------------------------------------------------------------------
def read_filaments(path):
    """Dosyadaki tüm plate'lerin filament kullanımını (tür, renk, gram) döndürür.
    Dilimlenmemişse boş liste döner."""
    toplam = {}
    with zipfile.ZipFile(path) as z:
        if "Metadata/slice_info.config" not in z.namelist():
            return []
        kok = ET.fromstring(z.read("Metadata/slice_info.config"))
        for plate in kok.iter("plate"):
            for fil in plate.iter("filament"):
                gram = float(fil.get("used_g", 0) or 0)
                if gram <= 0:
                    continue
                key = ((fil.get("type") or "").strip(),
                       (fil.get("color") or "")[:7].upper())
                toplam[key] = toplam.get(key, 0.0) + gram
    return [(t, c, g) for (t, c), g in toplam.items()]


# ----------------------------------------------------------------------------
# Veritabanı
# ----------------------------------------------------------------------------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        with self.con:
            self.con.executescript("""
            CREATE TABLE IF NOT EXISTS spools(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, type TEXT, color TEXT,
                initial_g REAL, remaining_g REAL, created TEXT);
            CREATE TABLE IF NOT EXISTS processed(
                key TEXT PRIMARY KEY, path TEXT, date TEXT);
            CREATE TABLE IF NOT EXISTS usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, file TEXT, spool_id INTEGER,
                type TEXT, color TEXT, grams REAL, status TEXT);
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT);
            """)

    # ayarlar
    def get(self, key, default=None):
        r = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def put(self, key, value):
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))

    # makaralar
    def spools(self):
        return self.con.execute("SELECT * FROM spools ORDER BY type, name, id").fetchall()

    def add_spool(self, d):
        with self.con:
            self.con.execute(
                "INSERT INTO spools(name,type,color,initial_g,remaining_g,created) VALUES(?,?,?,?,?,?)",
                (d["name"], d["type"], d["color"], d["initial"], d["remaining"], now()))

    def update_spool(self, sid, d):
        with self.con:
            self.con.execute(
                "UPDATE spools SET name=?,type=?,color=?,initial_g=?,remaining_g=? WHERE id=?",
                (d["name"], d["type"], d["color"], d["initial"], d["remaining"], sid))

    def delete_spool(self, sid):
        with self.con:
            self.con.execute("DELETE FROM spools WHERE id=?", (sid,))

    # işlenmiş dosyalar
    def is_processed(self, key):
        return self.con.execute("SELECT 1 FROM processed WHERE key=?", (key,)).fetchone() is not None

    def mark(self, key, path):
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO processed VALUES(?,?,?)", (key, path, now()))

    # kullanım
    def note(self, file, status, typ="", color="", grams=0.0):
        with self.con:
            self.con.execute(
                "INSERT INTO usage(date,file,spool_id,type,color,grams,status) VALUES(?,?,?,?,?,?,?)",
                (now(), file, None, typ, color, grams, status))

    def deduct(self, file, typ, color, grams):
        """Aynı tür+renkteki makaralardan (eskiden yeniye) düşer.
        Yetmeyen kısım 'stok yok' olarak kaydedilir. Eksik kalan gramı döndürür."""
        color = color.upper()
        rows = self.con.execute(
            "SELECT * FROM spools WHERE UPPER(color)=? AND remaining_g>0 "
            "AND (?='' OR UPPER(type)=UPPER(?)) ORDER BY id", (color, typ, typ)).fetchall()
        kalan = grams
        with self.con:
            for r in rows:
                if kalan <= 0:
                    break
                al = min(r["remaining_g"], kalan)
                self.con.execute("UPDATE spools SET remaining_g=remaining_g-? WHERE id=?", (al, r["id"]))
                self.con.execute(
                    "INSERT INTO usage(date,file,spool_id,type,color,grams,status) VALUES(?,?,?,?,?,?,?)",
                    (now(), file, r["id"], typ, color, al, "düşüldü"))
                kalan -= al
            if kalan > 0.005:
                self.con.execute(
                    "INSERT INTO usage(date,file,spool_id,type,color,grams,status) VALUES(?,?,?,?,?,?,?)",
                    (now(), file, None, typ, color, kalan, "stok yok"))
        return max(kalan, 0.0)

    def deducted_files(self):
        rows = self.con.execute(
            "SELECT DISTINCT file FROM usage WHERE status IN ('düşüldü','stok yok')").fetchall()
        return {r["file"] for r in rows}

    def usage(self, limit=500):
        return self.con.execute(
            "SELECT u.*, s.name AS sname FROM usage u LEFT JOIN spools s ON s.id=u.spool_id "
            "ORDER BY u.id DESC LIMIT ?", (limit,)).fetchall()

    def undo(self, uid):
        u = self.con.execute("SELECT * FROM usage WHERE id=?", (uid,)).fetchone()
        if not u or u["status"] not in ("düşüldü", "stok yok"):
            return False
        with self.con:
            if u["status"] == "düşüldü" and u["spool_id"] is not None:
                self.con.execute("UPDATE spools SET remaining_g=remaining_g+? WHERE id=?",
                                 (u["grams"], u["spool_id"]))
            self.con.execute("UPDATE usage SET status='geri alındı' WHERE id=?", (uid,))
        return True


# ----------------------------------------------------------------------------
# Makara ekleme/düzenleme penceresi
# ----------------------------------------------------------------------------
class SpoolDialog(QDialog):
    def __init__(self, parent=None, spool=None, prefill=None):
        super().__init__(parent)
        self.is_new = spool is None
        self.setWindowTitle("Yeni makara" if self.is_new else "Makarayı düzenle")

        self.name = QLineEdit()
        self.name.setPlaceholderText("örn. Bambu Lab Kırmızı")
        self.type = QComboBox()
        self.type.setEditable(True)
        self.type.addItems(TYPES)

        self.color_edit = QLineEdit("#FFFFFF")
        self.color_edit.setMaximumWidth(100)
        self.color_btn = QPushButton("Renk seç")
        self.color_edit.textChanged.connect(self._paint_btn)
        self.color_btn.clicked.connect(self._pick_color)
        crow = QHBoxLayout()
        crow.addWidget(self.color_edit)
        crow.addWidget(self.color_btn)

        self.remaining = self._spin(1000)

        src = spool or prefill
        if src:
            self.name.setText(src.get("name", ""))
            self.type.setCurrentText(src.get("type", "PLA") or "PLA")
            self.color_edit.setText(src.get("color", "#FFFFFF") or "#FFFFFF")
            if "remaining" in src:
                self.remaining.setValue(src["remaining"])

        form = QFormLayout()
        form.addRow("Ad / marka:", self.name)
        form.addRow("Tür:", self.type)
        form.addRow("Renk:", crow)
        form.addRow("Miktar:", self.remaining)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)
        self._paint_btn()

    @staticmethod
    def _spin(val):
        s = QDoubleSpinBox()
        s.setRange(0, 100000)
        s.setDecimals(2)
        s.setSuffix(" g")
        s.setValue(val)
        return s

    def _paint_btn(self):
        c = QColor(self.color_edit.text().strip())
        if c.isValid():
            fg = "#000" if c.lightness() > 128 else "#fff"
            self.color_btn.setStyleSheet(f"background:{c.name()}; color:{fg};")

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self.color_edit.text().strip()), self)
        if c.isValid():
            self.color_edit.setText(c.name().upper())

    def _accept(self):
        c = QColor(self.color_edit.text().strip())
        if not c.isValid():
            QMessageBox.warning(self, "Hata", "Geçerli bir renk gir (örn. #E6003A).")
            return
        if not self.type.currentText().strip():
            QMessageBox.warning(self, "Hata", "Filament türü boş olamaz.")
            return
        self.accept()

    def data(self):
        return {
            "name": self.name.text().strip(),
            "type": self.type.currentText().strip().upper(),
            "color": QColor(self.color_edit.text().strip()).name().upper(),
            "initial": self.remaining.value(),
            "remaining": self.remaining.value(),
        }


# ----------------------------------------------------------------------------
# Boş alana tıklayınca / Esc ile seçimi kaldıran tablo
# ----------------------------------------------------------------------------
class DeselectTable(QTableWidget):
    def _clear(self):
        self.clearSelection()
        self.setCurrentItem(None)

    def mousePressEvent(self, e):
        if not self.indexAt(e.pos()).isValid():
            self._clear()
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._clear()
        else:
            super().keyPressEvent(e)


# ----------------------------------------------------------------------------
# Ana pencere
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Filament Stok Takip")
        self.db = DB(DB_PATH)
        self.pending = {}
        self.timer = QTimer(self)
        self.timer.setInterval(SCAN_INTERVAL_MS)
        self.timer.timeout.connect(self.scan)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # üst çubuk: klasör + izleme
        top = QHBoxLayout()
        top.addWidget(QLabel("İzlenen klasör:"))
        self.folder_label = QLabel("(seçilmedi)")
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.folder_label, 1)
        btn_folder = QPushButton("Klasör seç")
        btn_folder.clicked.connect(self.choose_folder)
        top.addWidget(btn_folder)
        top.addWidget(QLabel("İşlenen dosyalar:"))
        self.after = QComboBox()
        self.after.addItem("Yerinde bırak", "keep")
        self.after.addItem(f"'{ARCHIVE_DIR}' klasörüne taşı", "move")
        self.after.addItem("Sil", "delete")
        idx = self.after.findData(self.db.get("after", "keep"))
        self.after.setCurrentIndex(max(idx, 0))
        self.after.currentIndexChanged.connect(self.on_after_changed)
        top.addWidget(self.after)
        btn_apply = QPushButton("Şimdi uygula")
        btn_apply.setToolTip("Daha önce işlenmiş dosyalara da seçili işlemi uygular")
        btn_apply.clicked.connect(self.apply_to_processed)
        top.addWidget(btn_apply)
        self.chk_watch = QCheckBox("İzlemeyi başlat")
        self.chk_watch.toggled.connect(self.toggle_watch)
        top.addWidget(self.chk_watch)
        root.addLayout(top)

        tabs = QTabWidget()
        root.addWidget(tabs)
        tabs.addTab(self._build_stock_tab(), "Stok")
        tabs.addTab(self._build_history_tab(), "Geçmiş")

        folder = self.db.get("folder", "")
        if folder:
            self.folder_label.setText(folder)
        self.refresh_all()
        if self.db.get("watching") == "1" and os.path.isdir(folder or ""):
            self.chk_watch.setChecked(True)

    # --- sekmeler ---------------------------------------------------------
    def _build_stock_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        for text, fn in (("Makara ekle", self.add_spool),
                         ("Düzenle", self.edit_spool),
                         ("Sil", self.delete_spool)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        self.summary = QLabel()
        bar.addWidget(self.summary)
        lay.addLayout(bar)

        self.stock = DeselectTable(0, 4)
        self.stock.setHorizontalHeaderLabels(["Renk", "Ad", "Tür", "Kalan (g)"])
        self._setup_table(self.stock)
        self.stock.doubleClicked.connect(self.edit_spool)
        lay.addWidget(self.stock)
        return w

    def _build_history_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        b1 = QPushButton("Seçili kullanımı geri al")
        b1.clicked.connect(self.undo_usage)
        b2 = QPushButton("Bu renk için makara ekle")
        b2.clicked.connect(self.add_from_usage)
        bar.addWidget(b1)
        bar.addWidget(b2)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.hist = DeselectTable(0, 7)
        self.hist.setHorizontalHeaderLabels(["Tarih", "Dosya", "Renk", "Tür", "Gram", "Makara", "Durum"])
        self._setup_table(self.hist)
        lay.addWidget(self.hist)
        return w

    @staticmethod
    def _setup_table(t):
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)

    # --- yenileme ---------------------------------------------------------
    def refresh_all(self):
        self.refresh_stock()
        self.refresh_history()

    def refresh_stock(self):
        rows = self.db.spools()
        self.stock.setRowCount(len(rows))
        toplam, dusuk = 0.0, 0
        for r, s in enumerate(rows):
            sw = QTableWidgetItem("")
            sw.setBackground(QColor(s["color"]))
            self.stock.setItem(r, 0, sw)
            name = QTableWidgetItem(s["name"] or "")
            name.setData(Qt.UserRole, s["id"])
            self.stock.setItem(r, 1, name)
            self.stock.setItem(r, 2, QTableWidgetItem(s["type"] or ""))
            rem = QTableWidgetItem(f"{s['remaining_g']:.1f}")
            if s["remaining_g"] < LOW_STOCK_G:
                rem.setForeground(QColor("#d32f2f"))
                dusuk += 1
            self.stock.setItem(r, 3, rem)
            toplam += s["remaining_g"]
        self.summary.setText(f"Toplam: {toplam:.0f} g  |  Azalan makara: {dusuk}")

    def refresh_history(self):
        rows = self.db.usage()
        self.hist.setRowCount(len(rows))
        for r, u in enumerate(rows):
            d = QTableWidgetItem(u["date"])
            d.setData(Qt.UserRole, u["id"])
            d.setData(Qt.UserRole + 1, {"type": u["type"] or "PLA", "color": u["color"] or "#FFFFFF"})
            self.hist.setItem(r, 0, d)
            self.hist.setItem(r, 1, QTableWidgetItem(u["file"] or ""))
            sw = QTableWidgetItem("")
            if u["color"]:
                sw.setBackground(QColor(u["color"]))
            self.hist.setItem(r, 2, sw)
            self.hist.setItem(r, 3, QTableWidgetItem(u["type"] or ""))
            self.hist.setItem(r, 4, QTableWidgetItem(f"{u['grams']:.2f}"))
            self.hist.setItem(r, 5, QTableWidgetItem(u["sname"] or "—"))
            st = QTableWidgetItem(u["status"])
            if u["status"] in ("stok yok", "okunamadı"):
                st.setForeground(QColor("#d32f2f"))
            elif u["status"] == "geri alındı":
                st.setForeground(QColor("#888888"))
            self.hist.setItem(r, 6, st)

    # --- makara işlemleri -------------------------------------------------
    def _selected_spool_id(self):
        row = self.stock.currentRow()
        if row < 0:
            return None
        return self.stock.item(row, 1).data(Qt.UserRole)

    def add_spool(self, prefill=None):
        if not isinstance(prefill, dict):
            prefill = None
        dlg = SpoolDialog(self, prefill=prefill)
        if dlg.exec_() != QDialog.Accepted:
            return
        d = dlg.data()

        # Aynı tür + renkte makara varsa üzerine eklemeyi öner
        mevcut = next((s for s in self.db.spools()
                       if (s["type"] or "").upper() == d["type"]
                       and (s["color"] or "").upper() == d["color"]), None)
        if mevcut:
            self.db.update_spool(mevcut["id"], {
                "name": mevcut["name"] or d["name"],
                "type": mevcut["type"],
                "color": mevcut["color"],
                "initial": mevcut["remaining_g"] + d["remaining"],
                "remaining": mevcut["remaining_g"] + d["remaining"],
            })
            self.refresh_stock()
            self.statusBar().showMessage(
                f"Mevcut makaranın üzerine eklendi: toplam {mevcut['remaining_g'] + d['remaining']:.0f} g", 6000)
            return

        self.db.add_spool(d)
        self.refresh_stock()

    def edit_spool(self, *_):
        sid = self._selected_spool_id()
        if sid is None:
            return
        s = next((x for x in self.db.spools() if x["id"] == sid), None)
        if not s:
            return
        spool = {"name": s["name"], "type": s["type"], "color": s["color"],
                 "initial": s["initial_g"], "remaining": s["remaining_g"]}
        dlg = SpoolDialog(self, spool=spool)
        if dlg.exec_() == QDialog.Accepted:
            self.db.update_spool(sid, dlg.data())
            self.refresh_stock()

    def delete_spool(self):
        sid = self._selected_spool_id()
        if sid is None:
            return
        if QMessageBox.question(self, "Sil", "Seçili makara silinsin mi?") == QMessageBox.Yes:
            self.db.delete_spool(sid)
            self.refresh_stock()
            self.refresh_history()

    # --- geçmiş işlemleri -------------------------------------------------
    def _selected_usage(self):
        row = self.hist.currentRow()
        if row < 0:
            return None, None
        it = self.hist.item(row, 0)
        return it.data(Qt.UserRole), it.data(Qt.UserRole + 1)

    def undo_usage(self):
        uid, _ = self._selected_usage()
        if uid is None:
            return
        if self.db.undo(uid):
            self.refresh_all()
        else:
            QMessageBox.information(self, "Bilgi", "Bu kayıt geri alınamaz.")

    def add_from_usage(self):
        _, info = self._selected_usage()
        if info:
            self.add_spool(prefill=info)

    # --- klasör izleme ----------------------------------------------------
    @staticmethod
    def _file_sig(path):
        st = os.stat(path)
        return (st.st_size, int(st.st_mtime))

    def _list_3mf(self, folder):
        out = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d.lower() != ARCHIVE_DIR]
            for fn in files:
                if fn.lower().endswith(".3mf"):
                    out.append(os.path.join(root, fn))
        return out

    @staticmethod
    def _key(path, sig):
        return f"{path}|{sig[0]}|{sig[1]}"

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "İzlenecek klasörü seç", self.db.get("folder", ""))
        if not folder:
            return
        self.db.put("folder", folder)
        self.folder_label.setText(folder)
        self.pending = {}

        mevcut = []
        for p in self._list_3mf(folder):
            try:
                sig = self._file_sig(p)
            except OSError:
                continue
            if not self.db.is_processed(self._key(p, sig)):
                mevcut.append((p, sig))
        if mevcut:
            cevap = QMessageBox.question(
                self, "Mevcut dosyalar",
                f"Klasörde {len(mevcut)} dosya var.\n\nBunlar da stoktan düşülsün mü?\n"
                "(Hayır: yalnızca bundan sonra gelen dosyalar işlenir.)")
            if cevap == QMessageBox.No:
                for p, sig in mevcut:
                    self.db.mark(self._key(p, sig), p)

    def toggle_watch(self, on):
        folder = self.db.get("folder", "")
        if on:
            if not os.path.isdir(folder or ""):
                QMessageBox.warning(self, "Hata", "Önce geçerli bir klasör seç.")
                self.chk_watch.blockSignals(True)
                self.chk_watch.setChecked(False)
                self.chk_watch.blockSignals(False)
                return
            self.timer.start()
            self.db.put("watching", "1")
            self.statusBar().showMessage("İzleme açık", 3000)
        else:
            self.timer.stop()
            self.pending = {}
            self.db.put("watching", "0")
            self.statusBar().showMessage("İzleme durdu", 3000)

    def scan(self):
        folder = self.db.get("folder", "")
        if not folder or not os.path.isdir(folder):
            return
        current = {}
        for p in self._list_3mf(folder):
            try:
                sig = self._file_sig(p)
            except OSError:
                continue
            key = self._key(p, sig)
            if not self.db.is_processed(key):
                current[p] = (key, sig)

        new_pending = {}
        for p, (key, sig) in current.items():
            # boyut/tarih bir önceki taramayla aynıysa kopyalama bitmiştir
            if self.pending.get(p) == sig:
                self.process(p, key)
            else:
                new_pending[p] = sig
        self.pending = new_pending

    def process(self, path, key):
        name = os.path.basename(path)
        try:
            items = read_filaments(path)
        except Exception as e:  # bozuk / yarım dosya
            self.db.note(name, "okunamadı")
            self.db.mark(key, path)
            self.statusBar().showMessage(f"{name} okunamadı: {e}", 8000)
            self.refresh_history()
            return

        if not items:
            self.db.note(name, "gramaj yok")
            self.statusBar().showMessage(f"{name}: dilimlenmemiş, düşülecek gramaj yok", 8000)
        else:
            eksik = 0.0
            toplam = 0.0
            for typ, color, gram in items:
                toplam += gram
                eksik += self.db.deduct(name, typ, color, gram)
            msg = f"{name}: {toplam:.2f} g düşüldü"
            if eksik > 0.005:
                msg += f"  |  UYARI: {eksik:.2f} g için eşleşen/yeterli makara yok"
            self.db.mark(key, path)
            msg += self.finish_file(path)
            self.statusBar().showMessage(msg, 10000)
        self.db.mark(key, path)
        self.refresh_all()

    def on_after_changed(self, _):
        mode = self.after.currentData()
        if mode == "delete":
            cevap = QMessageBox.question(
                self, "Dosyalar silinecek",
                "İşlenen dosyalar klasörden KALICI olarak silinir "
                "(geri dönüşüm kutusuna gitmez).\n\nEmin misin?")
            if cevap != QMessageBox.Yes:
                self.after.blockSignals(True)
                self.after.setCurrentIndex(self.after.findData(self.db.get("after", "keep")))
                self.after.blockSignals(False)
                return
        self.db.put("after", mode)

    def apply_to_processed(self):
        mode = self.after.currentData()
        if mode == "keep":
            QMessageBox.information(self, "Bilgi", "Önce 'taşı' veya 'Sil' seçeneğini seç.")
            return
        folder = self.db.get("folder", "")
        if not os.path.isdir(folder or ""):
            QMessageBox.warning(self, "Hata", "Önce geçerli bir klasör seç.")
            return
        done = self.db.deducted_files()
        hedef = []
        for p in self._list_3mf(folder):
            try:
                sig = self._file_sig(p)
            except OSError:
                continue
            if os.path.basename(p) in done and self.db.is_processed(self._key(p, sig)):
                hedef.append(p)
        if not hedef:
            QMessageBox.information(self, "Bilgi", "İşlem yapılacak, daha önce stoktan düşülmüş dosya bulunamadı.")
            return
        eylem = "KALICI olarak silinecek" if mode == "delete" else f"'{ARCHIVE_DIR}' klasörüne taşınacak"
        if QMessageBox.question(self, "Onay", f"{len(hedef)} dosya {eylem}.\n\nDevam edilsin mi?") != QMessageBox.Yes:
            return
        hatalar = []
        for p in hedef:
            sonuc = self.finish_file(p)
            if "taşınamadı" in sonuc:
                hatalar.append(f"{os.path.basename(p)}: {sonuc.split(': ', 1)[-1]}")
        if hatalar:
            QMessageBox.warning(self, "Bazı dosyalara uygulanamadı", "\n".join(hatalar[:10]))
        self.statusBar().showMessage(f"{len(hedef) - len(hatalar)} dosya işlendi", 8000)

    def finish_file(self, path):
        """Stoktan düşülen dosyayı ayara göre taşır/siler. Durum metni döndürür."""
        mode = self.after.currentData()
        if mode == "keep":
            return ""
        try:
            if mode == "delete":
                os.remove(path)
                return "  |  dosya silindi"
            dest_dir = os.path.join(self.db.get("folder", ""), ARCHIVE_DIR)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(path))
            if os.path.exists(dest):
                base, ext = os.path.splitext(os.path.basename(path))
                dest = os.path.join(dest_dir, f"{base}_{datetime.now():%Y%m%d_%H%M%S}{ext}")
            shutil.move(path, dest)
            return f"  |  '{ARCHIVE_DIR}' klasörüne taşındı"
        except OSError as e:
            return f"  |  dosya taşınamadı/silinemedi: {e}"


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(1000, 600)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()