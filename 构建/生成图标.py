"""生成应用图标（PNG + Windows ICO）—— 纯代码画，不依赖任何图片素材。

用法：运行环境/venv/bin/python 构建/生成图标.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QImage, QLinearGradient, QPainter,
                          QPainterPath, QPen)

输出 = Path(__file__).resolve().parent / "包内容" / "assets"


def 画图标(边长: int) -> QImage:
    图 = QImage(边长, 边长, QImage.Format.Format_ARGB32_Premultiplied)
    图.fill(Qt.GlobalColor.transparent)
    笔 = QPainter(图)
    笔.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # 圆角底：蓝紫渐变
    底 = QRectF(0, 0, 边长, 边长)
    渐变 = QLinearGradient(QPointF(0, 0), QPointF(边长, 边长))
    渐变.setColorAt(0.0, QColor("#5b8cff"))
    渐变.setColorAt(1.0, QColor("#7d4dff"))
    路径 = QPainterPath()
    路径.addRoundedRect(底.adjusted(边长 * 0.02, 边长 * 0.02,
                                 -边长 * 0.02, -边长 * 0.02),
                     边长 * 0.22, 边长 * 0.22)
    笔.fillPath(路径, QBrush(渐变))

    # 云：三个圆 + 一个圆角矩形
    白 = QColor(255, 255, 255, 240)
    笔.setPen(Qt.PenStyle.NoPen)
    笔.setBrush(QBrush(白))
    云底 = 边长 * 0.62
    笔.drawEllipse(QPointF(边长 * 0.36, 云底 - 边长 * 0.10), 边长 * 0.13, 边长 * 0.13)
    笔.drawEllipse(QPointF(边长 * 0.52, 云底 - 边长 * 0.15), 边长 * 0.16, 边长 * 0.16)
    笔.drawEllipse(QPointF(边长 * 0.66, 云底 - 边长 * 0.08), 边长 * 0.11, 边长 * 0.11)
    笔.drawRoundedRect(QRectF(边长 * 0.24, 云底 - 边长 * 0.06,
                            边长 * 0.52, 边长 * 0.16),
                     边长 * 0.08, 边长 * 0.08)

    # 向下的箭头（代表"下载/互传"）
    笔.setPen(QPen(白, 边长 * 0.075, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    笔.setBrush(Qt.BrushStyle.NoBrush)
    中心x = 边长 * 0.5
    笔.drawLine(QPointF(中心x, 边长 * 0.72), QPointF(中心x, 边长 * 0.90))
    笔.drawPolyline([QPointF(中心x - 边长 * 0.075, 边长 * 0.83),
                   QPointF(中心x, 边长 * 0.90),
                   QPointF(中心x + 边长 * 0.075, 边长 * 0.83)])
    笔.end()
    return 图


def 写ico(png们: list[Path], 目标: Path) -> None:
    """把若干 PNG 打包成 .ico（ICO 允许直接内嵌 PNG，比 BMP 省事且支持透明）。"""
    图 = [p.read_bytes() for p in png们]
    条目 = b""
    偏移 = 6 + 16 * len(png们)
    for p, 数据 in zip(png们, 图):
        边长 = int(p.stem.split("x")[-1])
        尺寸 = 0 if 边长 >= 256 else 边长
        条目 += struct.pack("<BBBBHHII", 尺寸, 尺寸, 0, 0, 1, 32, len(数据), 偏移)
        偏移 += len(数据)
    目标.write_bytes(struct.pack("<HHH", 0, 1, len(png们)) + 条目 + b"".join(图))


def main() -> int:
    输出.mkdir(parents=True, exist_ok=True)
    尺寸表 = [16, 24, 32, 48, 64, 128, 256]
    png们 = []
    for 边长 in 尺寸表:
        路径 = 输出 / f"icon-{边长}x{边长}.png"
        画图标(边长).save(str(路径), "PNG")
        png们.append(路径)
    (输出 / "icon.png").write_bytes((输出 / "icon-256x256.png").read_bytes())
    写ico(png们, 输出 / "icon.ico")
    for p in png们:
        if p.name != "icon-256x256.png":
            p.unlink()
    print(f"已生成：{输出 / 'icon.png'}、{输出 / 'icon.ico'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
