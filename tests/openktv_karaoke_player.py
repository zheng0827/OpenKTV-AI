# -*- coding: utf-8 -*-
import sys
from pathlib import Path
from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSlider, QFileDialog, QMessageBox, QCheckBox
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget


class LyricWord:
    def __init__(self, start, end, text):
        self.start = float(start)
        self.end = float(end)
        self.text = text


class LyricLine:
    def __init__(self, start, end, text):
        self.start = float(start)
        self.end = float(end)
        self.text = text
        self.words = []


def parse_ktv_lrc(path):
    lines, dialogue = [], []
    current = None
    text = Path(path).read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("@"):
            continue
        if s[0] not in "%$&":
            continue
        p = s[1:].strip().split(maxsplit=2)
        if len(p) < 3:
            continue
        try:
            start, end = float(p[0]), float(p[1])
        except ValueError:
            continue
        content = p[2]
        if s[0] == "%":
            current = LyricLine(start, end, content)
            lines.append(current)
        elif s[0] == "$" and current is not None:
            current.words.append(LyricWord(start, end, content))
        elif s[0] == "&":
            dialogue.append(LyricLine(start, end, content))
    return lines, dialogue


class KaraokeLyrics(QWidget):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.dialogue = []
        self.t = 0.0
        self.show_dialogue = False
        self.setMinimumHeight(180)

    def set_data(self, lines, dialogue):
        self.lines, self.dialogue = lines, dialogue
        self.update()

    def set_time(self, seconds):
        self.t = seconds
        self.update()

    def set_dialogue_visible(self, value):
        self.show_dialogue = value
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(6, 6, 6, 235))
        if not self.lines:
            p.setPen(QColor("#bbbbbb"))
            p.setFont(QFont("Microsoft JhengHei", 24, QFont.Bold))
            p.drawText(self.rect(), Qt.AlignCenter, "請載入 KTV-LRC 歌詞")
            return

        idx = self._current_index()
        if idx is None:
            return
        if idx > 0:
            self._draw_center(p, self.lines[idx-1].text, self.height()//2 - 70,
                              QColor("#666666"), 17)
        self._draw_karaoke_line(p, self.lines[idx], self.height()//2)
        if idx + 1 < len(self.lines):
            self._draw_center(p, self.lines[idx+1].text, self.height()//2 + 65,
                              QColor("#666666"), 17)

        if self.show_dialogue:
            for d in self.dialogue:
                if d.start <= self.t <= d.end:
                    self._draw_center(p, "【對白】 " + d.text, 24,
                                      QColor("#cccccc"), 15)
                    break

    def _current_index(self):
        for i, line in enumerate(self.lines):
            if line.start <= self.t <= line.end:
                return i
        for i, line in enumerate(self.lines):
            if self.t < line.start:
                return max(0, i)
        return len(self.lines) - 1

    def _draw_center(self, p, text, baseline_center, color, size):
        font = QFont("Microsoft JhengHei", size, QFont.Bold)
        p.setFont(font)
        r = self.rect()
        r = r.adjusted(20, 0, -20, 0)
        p.setPen(QColor(0, 0, 0, 230))
        for ox, oy in [(-2,-2),(0,-2),(2,-2),(-2,0),(2,0),(-2,2),(0,2),(2,2)]:
            p.drawText(r.translated(ox, oy + baseline_center - self.height()//2),
                       Qt.AlignHCenter, text)
        p.setPen(color)
        p.drawText(r.translated(0, baseline_center - self.height()//2),
                   Qt.AlignHCenter, text)

    def _draw_karaoke_line(self, p, line, center_y):
        font = QFont("Microsoft JhengHei", 31, QFont.Bold)
        p.setFont(font)
        metrics = p.fontMetrics()
        if not line.words:
            self._draw_center(p, line.text, center_y, QColor("#f5f5f5"), 31)
            return

        widths = [metrics.horizontalAdvance(w.text) for w in line.words]
        total = sum(widths)
        x = (self.width() - total) / 2
        baseline = center_y + 11

        for word, width in zip(line.words, widths):
            if self.t <= word.start:
                ratio = 0.0
            elif self.t >= word.end:
                ratio = 1.0
            else:
                ratio = (self.t - word.start) / max(0.001, word.end - word.start)
                ratio = max(0.0, min(1.0, ratio))

            # normal text
            self._draw_word(p, word.text, x, baseline, QColor("#f5f5f5"), font)
            # karaoke sweep
            if ratio > 0:
                p.save()
                p.setClipRect(int(x), 0, max(1, int(width * ratio)), self.height())
                self._draw_word(p, word.text, x, baseline, QColor("#ffd23f"), font)
                p.restore()
            x += width

    def _draw_word(self, p, text, x, baseline, color, font):
        p.setFont(font)
        p.setPen(QColor(0, 0, 0, 235))
        for ox, oy in [(-2,-2),(0,-2),(2,-2),(-2,0),(2,0),(-2,2),(0,2),(2,2)]:
            p.drawText(int(x+ox), int(baseline+oy), text)
        p.setPen(color)
        p.drawText(int(x), int(baseline), text)


class Player(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenKTV - Karaoke Player")
        self.resize(1280, 820)
        self.media = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.media.setAudioOutput(self.audio)
        self.video = QVideoWidget(self)
        self.media.setVideoOutput(self.video)
        self.lyrics = KaraokeLyrics()
        self.open_video_btn = QPushButton("📹 開啟影片")
        self.open_lrc_btn = QPushButton("📝 開啟 KTV-LRC")
        self.play_btn = QPushButton("▶ 播放")
        self.back_btn = QPushButton("⏪ -5 秒")
        self.next_btn = QPushButton("+5 秒 ⏩")
        self.dialogue_check = QCheckBox("顯示 MV 對白")
        self.slider = QSlider(Qt.Horizontal)
        self.time_label = QLabel("00:00 / 00:00")
        self.status = QLabel("請開啟影片與 .lrc")
        self._build()
        self._bind()
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.video, 1)
        root.addWidget(self.lyrics)
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 8, 10, 8)
        for w in [self.open_video_btn, self.open_lrc_btn, self.back_btn, self.play_btn,
                  self.next_btn, self.time_label, self.dialogue_check]:
            bar.addWidget(w)
        root.addLayout(bar)
        root.addWidget(self.slider)
        root.addWidget(self.status)

    def _bind(self):
        self.open_video_btn.clicked.connect(self.open_video)
        self.open_lrc_btn.clicked.connect(self.open_lrc)
        self.play_btn.clicked.connect(self.toggle_play)
        self.back_btn.clicked.connect(lambda: self.seek_relative(-5000))
        self.next_btn.clicked.connect(lambda: self.seek_relative(5000))
        self.slider.sliderMoved.connect(self.media.setPosition)
        self.media.positionChanged.connect(self.position_changed)
        self.media.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.media.playbackStateChanged.connect(self.state_changed)
        self.dialogue_check.stateChanged.connect(
            lambda s: self.lyrics.set_dialogue_visible(s == Qt.Checked)
        )

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇影片", "", "Video (*.mp4 *.mkv *.mov *.avi *.webm)"
        )
        if path:
            self.media.setSource(QUrl.fromLocalFile(path))
            self.status.setText(f"影片：{Path(path).name}")

    def open_lrc(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇 KTV-LRC", "", "KTV Lyrics (*.lrc)"
        )
        if not path:
            return
        try:
            lines, dialogue = parse_ktv_lrc(path)
            self.lyrics.set_data(lines, dialogue)
            self.status.setText(f"歌詞：{Path(path).name} | {len(lines)} 行")
        except Exception as e:
            QMessageBox.critical(self, "歌詞讀取失敗", str(e))

    def toggle_play(self):
        if self.media.playbackState() == QMediaPlayer.PlayingState:
            self.media.pause()
        else:
            self.media.play()

    def seek_relative(self, delta):
        self.media.setPosition(max(0, min(self.media.position()+delta, self.media.duration())))

    def position_changed(self, pos):
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        cur = max(0, pos // 1000)
        total = max(0, self.media.duration() // 1000)
        self.time_label.setText(f"{cur//60:02d}:{cur%60:02d} / {total//60:02d}:{total%60:02d}")
        self.lyrics.set_time(pos / 1000.0)

    def state_changed(self, state):
        self.play_btn.setText("⏸ 暫停" if state == QMediaPlayer.PlayingState else "▶ 播放")

    def _tick(self):
        # QMediaPlayer.positionChanged 已經負責同步；timer 用於低延遲刷新
        self.lyrics.set_time(self.media.position() / 1000.0)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = Player()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
