"""Tek QuickStart-Inspired Modern SMU Control Suite Stylesheet & Design Tokens."""

from __future__ import annotations

GLOBAL_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #f1f5f9;
    font-family: 'Malgun Gothic', '맑은 고딕', 'Segoe UI', sans-serif;
    font-size: 9pt;
    color: #0f172a;
}

QGroupBox {
    background-color: #ffffff;
    border: 1.5px solid #cbd5e1;
    border-radius: 7px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: bold;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    background-color: #ffffff;
}

QGroupBox#colInput {
    border: 1.8px solid #3b82f6;
}
QGroupBox#colInput::title {
    color: #1d4ed8;
}

QGroupBox#colMetrics {
    border: 1.8px solid #10b981;
}
QGroupBox#colMetrics::title {
    color: #047857;
}

QGroupBox#colGraph {
    border: 1.8px solid #8b5cf6;
}
QGroupBox#colGraph::title {
    color: #6d28d9;
}

QTabWidget::pane {
    border: 1px solid #cbd5e1;
    border-radius: 5px;
    background-color: #ffffff;
}

QTabBar::tab {
    background-color: #e2e8f0;
    color: #334155;
    padding: 6px 12px;
    margin-right: 3px;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    font-weight: bold;
    font-size: 8.8pt;
}

QTabBar::tab:selected {
    background-color: #2563eb;
    color: #ffffff;
}

QTabBar::tab:hover:!selected {
    background-color: #cbd5e1;
}

QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
    background-color: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 3px 6px;
    color: #0f172a;
}

QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {
    border: 1.5px solid #2563eb;
}

QTableWidget {
    background-color: #ffffff;
    alternate-background-color: #f8fafc;
    gridline-color: #e2e8f0;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    font-size: 8.5pt;
}

QHeaderView::section {
    background-color: #f1f5f9;
    color: #1e293b;
    font-weight: bold;
    padding: 4px 6px;
    border: none;
    border-right: 1px solid #cbd5e1;
    border-bottom: 1px solid #cbd5e1;
}

QPushButton {
    background-color: #f8fafc;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 4px 10px;
    color: #0f172a;
}

QPushButton:hover {
    background-color: #e2e8f0;
}

QScrollArea {
    border: none;
    background-color: transparent;
}
"""
