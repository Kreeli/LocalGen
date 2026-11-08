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
QLabel#brand { font-size: 24px; font-weight: 800; color: #f0edff; }
QLabel#heading { font-size: 18px; font-weight: 700; }
QLabel#caption { font-size: 12px; color: #9ca7c0; }
QLabel#engine { padding: 7px 11px; background: #222839; border-radius: 8px; color: #c8c2ff; }
QLabel#notice { color: #e4c393; }
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
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus { border-color: #9b87ff; }
QComboBox QAbstractItemView { background: #242a3c; selection-background-color: #57429e; }
QListWidget { background: #141824; border: 1px solid #2c3449; border-radius: 7px; outline: 0; }
QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #242b3d; }
QListWidget::item:selected { background: #393252; color: #ffffff; }
QProgressBar { background: #242a3c; border: 0; border-radius: 4px; height: 6px; text-align: center; }
QProgressBar::chunk { background: #a591ff; border-radius: 4px; }
"""
