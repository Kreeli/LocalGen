STYLE = """
QWidget {
    background: #12141d;
    color: #e7e9f4;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QLabel { background: transparent; }
QFrame#card, QFrame#header { background: #191d29; border: 1px solid #2a3041; border-radius: 12px; }
QFrame#canvas { background: #10131e; border: 1px dashed #323a51; border-radius: 10px; }
QFrame#canvas[filled="yes"] { border: 1px solid #262d3f; }
QLabel#referenceThumb { background: #10131e; border: 1px solid #2c3449; border-radius: 8px;
    font-size: 11px; color: #79839b; }
QLabel#brand { font-size: 24px; font-weight: 800; color: #f0edff; }
QLabel#heading { font-size: 18px; font-weight: 700; }
QLabel#caption { font-size: 12px; color: #9ca7c0; }
QLabel#engine { padding: 7px 11px; background: #222839; border-radius: 8px; color: #c8c2ff; }
QLabel#notice { color: #e4c393; }
QLabel#currentJob { padding: 5px 10px; background: #222839; border-radius: 7px;
    color: #c8c2ff; font-size: 12px; }
QLabel#placeholder { color: #aab5cc; font-size: 15px; }
QPushButton, QComboBox, QLineEdit, QPlainTextEdit {
    background: #141824; border: 1px solid #333c54; border-radius: 7px;
    padding: 8px 10px; color: #e7e9f4; selection-background-color: #6049ad;
}
QPushButton { background: #242a3c; font-weight: 600; }
QPushButton:hover { background: #333a50; border-color: #666994; }
QPushButton:disabled { background: #1e2230; color: #79839b; border-color: #292e3d; }
QPushButton#primary { background: #9b87ff; border-color: #a997ff; color: #141022; font-size: 14px; padding: 12px; }
QPushButton#primary:hover { background: #b1a1ff; }
QPushButton#primary:disabled { background: #2b2941; border-color: #3b3558; color: #9891b3; }
QPushButton#ghost { background: transparent; border: 1px solid #333c54; padding: 3px 9px;
    font-size: 12px; font-weight: 500; color: #c3cbe0; }
QPushButton#ghost:hover { background: #232a3d; border-color: #6d74a0; }
QPushButton#ghost:disabled { background: transparent; color: #5d6580; border-color: #262c3c; }
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus { border-color: #9b87ff; }
QComboBox QAbstractItemView { background: #242a3c; selection-background-color: #57429e; }
QListWidget { background: #141824; border: 1px solid #2c3449; border-radius: 7px; outline: 0; }
QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #242b3d; }
QListWidget::item:selected { background: #393252; color: #ffffff; }
QProgressBar { background: #242a3c; border: 0; border-radius: 4px; height: 6px; text-align: center; }
QProgressBar::chunk { background: #a591ff; border-radius: 4px; }
QScrollArea#editor { background: transparent; border: 0; }
QScrollArea#editor > QWidget > QWidget { background: transparent; }
QScrollArea#detail { background: transparent; border: 0; }
QScrollArea#detail > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 4px 2px 4px 2px; }
QScrollBar::handle:vertical { background: #333c54; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: #4b5573; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QSplitter#split::handle { background: transparent; }
QSplitter#columns::handle { background: transparent; }
QSplitter#history::handle { background: transparent; }
QMessageBox { background: #191d29; }
QMessageBox QLabel { color: #e7e9f4; }
QFrame#reference { background: #141824; border: 1px dashed #333c54; border-radius: 10px; }
QFrame#reference[locked="yes"] { background: #14161f; border: 1px dashed #262c3c; }
QFrame#libraryPicker { background: #141824; border: 1px solid #2c3449; border-radius: 10px; }
QFrame#libraryPicker:disabled { background: #12141d; border-color: #222736; }
QLabel#libraryThumb { background: #10131e; border: 1px dashed #323a51; border-radius: 8px;
    font-size: 11px; color: #79839b; }
QLabel#libraryThumb[filled="yes"] { border: 1px solid #3a4360; }
QLabel#libraryName { font-weight: 600; color: #dfe4f2; font-size: 12px; }
QLabel#libraryHint { font-size: 11px; color: #8f9ab4; }
QToolButton#libraryCard { background: #141824; border: 1px solid #2c3449; border-radius: 8px;
    font-size: 11px; font-weight: 500; color: #c3cbe0; padding: 3px 2px; text-align: center; }
QToolButton#libraryCard:hover { border-color: #6d74a0; background: #1c2131; }
QToolButton#libraryCard[selected="true"] { border: 1px solid #9b87ff; background: #241f3a; }
QLabel#referenceName { font-weight: 600; color: #dfe4f2; }
QLabel#referenceHint { font-size: 11px; color: #e4c393; }
QSlider::groove:horizontal { height: 4px; background: #262c3c; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #a591ff; border-radius: 2px; }
QSlider::handle:horizontal { background: #e7e9f4; width: 13px; margin: -5px 0;
    border-radius: 6px; }
QSlider::handle:horizontal:hover { background: #ffffff; }
QSlider:disabled::sub-page:horizontal { background: #3a3f52; }
"""
