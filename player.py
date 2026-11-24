from contextlib import nullcontext
from pathlib import Path
from random import shuffle
import sys

from PySide6.QtCore import QDir, QEvent, QPointF, QSettings, QSignalBlocker, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QCursor,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QSurfaceFormat,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame
from PySide6.QtQuick import QQuickView
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QProxyStyle,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStyle,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from panorama import DEFAULT_FOV, PANORAMA_KEYS, PanoramaView
import playlist_ipc


SEEK_MS = 5_000
RIGHT_HOLD_MS = 350
DEFAULT_HOLD_RATE = 2.0
RATE_STEP = 0.5
PLAYBACK_RATES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
HOLD_RATES = PLAYBACK_RATES[2:]
VOLUME_STEP = 5
WHEEL_NOTCH = 120
DEFAULT_PLAYLIST_WIDTH = 234
FULLSCREEN_CONTROLS_EDGE = 48
FULLSCREEN_CONTROLS_HIDE_MS = 450
APP_NAME = "简放"
ICON_COLORS = ((QIcon.Mode.Normal, "#d7dce4"), (QIcon.Mode.Disabled, "#858d98"))
MEDIA_FILTER = (
    "媒体文件 (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.ts *.mpeg *.mpg *.wmv "
    "*.mp3 *.flac *.wav *.m4a *.aac *.ogg *.opus *.wma *.aiff);;所有文件 (*)"
)


class Playlist:
    def __init__(self):
        self.files: list[Path] = []
        self.order: list[int] = []
        self.current: int | None = None
        self.shuffled = False

    def replace(self, files: list[Path]):
        self.files = files
        self.current = None
        self.set_shuffle(self.shuffled)

    def append(self, files: list[Path]):
        new_rows = list(range(len(self.files), len(self.files) + len(files)))
        if self.shuffled:
            shuffle(new_rows)
        self.files.extend(files)
        self.order.extend(new_rows)

    def set_shuffle(self, enabled: bool):
        self.shuffled = enabled
        self.order = list(range(len(self.files)))
        if enabled:
            if self.current is not None:
                self.order.remove(self.current)
            shuffle(self.order)
            if self.current is not None:
                self.order.insert(0, self.current)

    def adjacent(self, offset: int) -> int | None:
        if self.current is None:
            return None
        index = self.order.index(self.current) + offset
        if 0 <= index < len(self.order):
            return self.order[index]
        return None


class VideoSurface(QWidget):
    clicked = Signal()
    double_clicked = Signal()
    panorama_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("videoSurface")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.panorama_active = False
        self.panorama_drag = None

        # A native Qt Quick window can use D3D11 video textures beside the OpenGL panorama.
        self.flat_window = QQuickView()
        self.flat_window.setFlags(Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.flat_window.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        self.flat_window.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name("flat_video.qml"))))
        self.flat_scene = self.flat_window.rootObject()
        if self.flat_scene is None:
            details = "\n".join(error.toString() for error in self.flat_window.errors())
            raise RuntimeError(f"无法加载普通视频画面\n{details}")
        self.video_sink = self.flat_scene.property("videoSink")
        self.video_sink.videoFrameChanged.connect(self.update_panorama_frame)
        self.flat_video = QWidget.createWindowContainer(self.flat_window, self, Qt.WindowType.WindowTransparentForInput)
        self.flat_video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.flat_video.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.flat_video.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.flat_video.hide()

        self.message = QLabel(self)
        self.message.setObjectName("surfaceMessage")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.speed = QLabel(self)
        self.speed.setObjectName("speed")
        self.speed.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.speed.hide()

        self.volume = QLabel(self)
        self.volume.setObjectName("volume")
        self.volume.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.volume.move(16, 16)
        self.volume.hide()
        self.volume_timer = QTimer(self, singleShot=True, interval=1200)
        self.volume_timer.timeout.connect(self.hide_volume)

        # Add the OpenGL child before the top-level window has a native handle.
        self.panorama = PanoramaView(self)
        self.panorama.hide()
        self.panorama.failed.connect(self.panorama_failed.emit, Qt.ConnectionType.QueuedConnection)
        self.volume.raise_()
        self.speed.raise_()

        self.click_timer = QTimer(self, singleShot=True)
        self.click_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.click_timer.timeout.connect(self.clicked)
        self.click_on_release = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.flat_video.setGeometry(self.rect())
        self.message.setGeometry(self.rect().adjusted(24, 12, -24, -12))
        self.panorama.setGeometry(self.rect())
        self.position_speed()

    def show_message(self, text: str):
        self.message.setText(text)
        self.message.setVisible(bool(text))
        self.flat_video.setVisible(not text and not self.panorama_active)

    def set_panorama(self, enabled: bool):
        self.panorama_active = enabled
        self.click_timer.stop()
        self.reset_pointer()
        if enabled:
            self.flat_video.hide()
            self.panorama.show()
            self.panorama.submit_frame(self.video_sink.videoFrame())
            QTimer.singleShot(0, self.panorama.check_context)
        else:
            self.panorama.suspend()
            self.flat_video.setVisible(not self.message.text())

    @Slot(QVideoFrame)
    def update_panorama_frame(self, frame: QVideoFrame):
        if self.panorama_active:
            self.panorama.submit_frame(frame)

    def reset_pointer(self):
        self.click_on_release = False
        self.panorama_drag = None
        if self.panorama_active:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def show_speed(self, rate: float):
        text = f"{rate:g}×"
        self.flat_scene.setProperty("speedText", text if rate != 1.0 else "")
        self.speed.setText(text)
        self.speed.setVisible(rate != 1.0)
        self.position_speed()

    def position_speed(self):
        self.speed.adjustSize()
        self.speed.move(self.width() - self.speed.width() - 16, 16)

    def show_volume(self, percent: int):
        text = f"音量 {percent}%"
        self.flat_scene.setProperty("volumeText", text)
        self.volume.setText(text)
        self.volume.adjustSize()
        self.volume.show()
        self.volume_timer.start()

    def hide_volume(self):
        self.flat_scene.setProperty("volumeText", "")
        self.volume.hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.click_on_release = True
            if self.panorama_active:
                self.panorama_drag = event.position()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.panorama_drag is None:
            super().mouseMoveEvent(event)
            return
        delta = event.position() - self.panorama_drag
        if not self.click_on_release or delta.manhattanLength() >= QApplication.startDragDistance():
            self.click_on_release = False
            self.click_timer.stop()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.panorama.drag(delta.x(), delta.y())
            self.panorama_drag = event.position()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.click_on_release and self.rect().contains(event.position().toPoint()):
                self.click_timer.start(QApplication.doubleClickInterval())
            self.reset_pointer()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_pointer()
            self.click_timer.stop()
            self.double_clicked.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class PlayerStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, return_data=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return Qt.MouseButton.LeftButton.value
        return super().styleHint(hint, option, widget, return_data)


def control_icon(style: QStyle, symbol: QStyle.StandardPixmap) -> QIcon:
    standard = style.standardIcon(symbol)
    icon = QIcon()
    for mode, color in ICON_COLORS:
        pixmap = standard.pixmap(32, 32)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(color))
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


def option_icon(strokes, *, ring=False) -> QIcon:
    path = QPainterPath()
    for start, *points in strokes:
        path.moveTo(QPointF(*start))
        for point in points:
            path.lineTo(QPointF(*point))
    if ring:
        path.addEllipse(QPointF(8, 8), 2.25, 2.25)
    icon = QIcon()
    for mode, color in ICON_COLORS:
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2, 2)
        pen = QPen(QColor(color), 1.35)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


OPEN_FOLDER_STROKES = (
    ((13.1, 7), (13.1, 5.6), (12.4, 4.9), (7.4, 4.9), (6, 3.5), (3, 3.5),
     (2.1, 4.4), (2.1, 11.8), (3, 12.7), (3.7, 12.7)),
    ((3.7, 12.7), (5, 7.7), (5.8, 7), (13.9, 7), (14.6, 7.8),
     (13.4, 11.9), (12.5, 12.7), (3.7, 12.7)),
)
REPEAT_STROKES = (
    ((2, 6), (2, 4.5), (3.5, 3), (14, 3)), ((11.5, 1), (14, 3), (11.5, 5)),
    ((14, 10), (14, 11.5), (12.5, 13), (2, 13)), ((4.5, 11), (2, 13), (4.5, 15)),
    ((7, 7), (8, 6), (8, 10)), ((7, 10), (9, 10)),
)
SHUFFLE_STROKES = (
    ((2, 4), (5, 4), (11, 12), (14, 12)), ((11.5, 9.75), (14, 12), (11.5, 14.25)),
    ((2, 12), (5, 12), (11, 4), (14, 4)), ((11.5, 1.75), (14, 4), (11.5, 6.25)),
)
SETTINGS_STROKES = (
    ((6.4, 1.4), (9.6, 1.4), (10, 3.3), (11.1, 4), (12.9, 3.4), (14.5, 6.1),
     (13.1, 7.4), (13.1, 8.6), (14.5, 9.9), (12.9, 12.6), (11.1, 12), (10, 12.7),
     (9.6, 14.6), (6.4, 14.6), (6, 12.7), (4.9, 12), (3.1, 12.6), (1.5, 9.9),
     (2.9, 8.6), (2.9, 7.4), (1.5, 6.1), (3.1, 3.4), (4.9, 4), (6, 3.3), (6.4, 1.4)),
)


class ShortcutHelpDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("shortcutHelp")
        self.setWindowTitle("快捷键与鼠标操作")
        self.resize(520, 455)
        self.setMinimumSize(400, 280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(12)
        scroll = QScrollArea()
        scroll.setObjectName("shortcutHelpScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("shortcutHelpBody")
        rows = QGridLayout(body)
        rows.setContentsMargins(0, 0, 12, 0)
        rows.setHorizontalSpacing(18)
        rows.setVerticalSpacing(6)
        rows.setColumnStretch(1, 1)
        sections = (
            ("播放与进度", (
                ("空格 / 单击画面", "播放或暂停"),
                ("← / 短按 →", f"后退 / 前进 {SEEK_MS // 1000} 秒"),
                ("双击画面 / Esc", "双击切换全屏，Esc 退出全屏"),
                ("Ctrl+O / 拖入文件", "在当前窗口打开文件"),
                ("长按 →", "临时以倍速播放，松开后恢复原倍速"),
            )),
            ("音量", (
                ("↑ / ↓ 或滚轮", f"每档调节 {VOLUME_STEP} 个百分点，滚轮在画面或控制栏生效"),
                ("拖动音量滑块", "直接调节音量"),
                ("单击扬声器图标", "静音或恢复原音量"),
                ("Z / X", f"减速 / 加速，每档 {RATE_STEP:g}×"),
            )),
            ("全景操作", (
                ("W / A / S / D", "向上 / 左 / 下 / 右转向"),
                ("按住左键拖动画面", "转动全景视角"),
                ("Q / E", "放大 / 缩小"),
            )),
        )
        row = 0
        for heading, shortcuts in sections:
            if row:
                rows.setRowMinimumHeight(row, 6)
                row += 1
            title = QLabel(heading)
            title.setObjectName("shortcutSection")
            rows.addWidget(title, row, 0, 1, 2)
            row += 1
            for operation, description in shortcuts:
                action_label = QLabel(description)
                action_label.setObjectName("shortcutAction")
                action_label.setWordWrap(True)
                rows.addWidget(QLabel(operation), row, 0)
                rows.addWidget(action_label, row, 1)
                row += 1
        rows.setRowStretch(row, 1)
        scroll.setWidget(body)
        layout.addWidget(scroll)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.reject)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)


class PlayerWindow(QMainWindow):
    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle(APP_NAME)
        self.resize(960, 600)
        self.setMinimumSize(680, 300)
        self.setAcceptDrops(True)
        self.playlist = Playlist()
        self.media_error = ""
        self.was_maximized = False
        self.right_down = False
        self.selected_rate = 1.0
        self.fast_playback = False
        self.hold_rate = self.saved_hold_rate()
        self.shortcut_help = None
        self.panorama_enabled = settings.value("panorama/enabled", False, type=bool)
        self.panorama_unavailable = False
        self.playlist_width = DEFAULT_PLAYLIST_WIDTH
        self.wheel_delta = 0

        self.hold_timer = QTimer(self, singleShot=True, interval=RIGHT_HOLD_MS)
        self.hold_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.hold_timer.timeout.connect(self.begin_fast_playback)

        self.controls_hide_timer = QTimer(self, singleShot=True, interval=FULLSCREEN_CONTROLS_HIDE_MS)
        self.controls_hide_timer.timeout.connect(self.hide_fullscreen_controls)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        volume = settings.value("playback/volume", 100, type=int)
        self.audio.setVolume(max(0, min(100, volume)) / 100)
        self.audio.setMuted(settings.value("playback/muted", False, type=bool))
        self.player.setAudioOutput(self.audio)
        self.build_interface()
        self.surface.panorama.set_fov(settings.value("panorama/fov", DEFAULT_FOV, type=float))
        self.surface.panorama.zoom_changed.connect(self.save_panorama_zoom)
        self.player.setVideoSink(self.surface.video_sink)
        self.player.hasVideoChanged.connect(self.update_surface)
        self.player.errorOccurred.connect(self.show_media_error)
        self.player.positionChanged.connect(self.update_position)
        self.player.durationChanged.connect(self.update_duration)
        self.player.seekableChanged.connect(self.progress.setEnabled)
        self.player.playbackStateChanged.connect(self.update_play_button)
        self.player.playbackRateChanged.connect(self.update_rate_display)
        self.player.mediaStatusChanged.connect(self.media_status_changed)
        self.audio.volumeChanged.connect(self.update_volume_display)
        self.audio.volumeChanged.connect(self.save_volume)
        self.audio.mutedChanged.connect(self.update_mute_button)
        self.audio.mutedChanged.connect(lambda muted: self.settings.setValue("playback/muted", muted))
        QApplication.instance().installEventFilter(self)
        self.update_play_modes()
        self.update_surface()
        self.update_navigation()
        self.update_play_button()
        self.update_rate_display()
        self.update_volume_display(self.audio.volume())
        self.update_mute_button(self.audio.isMuted())

    def build_interface(self):
        root = QWidget()
        self.setCentralWidget(root)
        self.root_layout = QVBoxLayout(root)
        self.root_layout.setContentsMargins(0, 0, 0, 0)
        self.root_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        self.root_layout.addWidget(self.splitter, 1)
        self.surface = VideoSurface()
        self.surface.setMinimumWidth(160)
        self.surface.clicked.connect(self.toggle_play)
        self.surface.double_clicked.connect(self.toggle_fullscreen)
        self.surface.panorama_failed.connect(self.handle_panorama_failure)
        self.surface.panorama.shutdown_finished.connect(self.close)
        self.splitter.addWidget(self.surface)

        self.panel = QWidget()
        self.panel.setObjectName("playlistPanel")
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.addWidget(QLabel("播放列表"))
        self.track_list = QListWidget()
        self.track_list.setAlternatingRowColors(True)
        self.track_list.itemActivated.connect(self.play_selected)
        panel_layout.addWidget(self.track_list, 1)
        self.splitter.addWidget(self.panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.handle(1).setToolTip("拖动调整播放列表宽度")
        self.splitter.splitterMoved.connect(self.remember_playlist_width)
        self.panel.hide()

        self.controls = QWidget()
        self.controls.setObjectName("controls")
        # The flat video is a native child window, so the overlay must stack natively too.
        self.controls.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.controls.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        bar = QHBoxLayout(self.controls)
        bar.setContentsMargins(8, 8, 8, 8)
        bar.setSpacing(4)
        self.root_layout.addWidget(self.controls)

        style = self.style()
        open_button = QPushButton(option_icon(OPEN_FOLDER_STROKES), "")
        open_button.setAccessibleName("打开文件")
        open_button.setToolTip("打开文件 (Ctrl+O)")
        open_button.clicked.connect(self.open_files)
        self.open_shortcut = QShortcut(QKeySequence.StandardKey.Open, self)
        self.open_shortcut.activated.connect(self.open_files)
        bar.addWidget(open_button)
        self.previous_button = QPushButton(control_icon(style, QStyle.StandardPixmap.SP_MediaSkipBackward), "")
        self.previous_button.setAccessibleName("上一首")
        self.previous_button.setToolTip("上一首")
        self.previous_button.clicked.connect(lambda: self.play_adjacent(-1))
        bar.addWidget(self.previous_button)
        self.play_icon = control_icon(style, QStyle.StandardPixmap.SP_MediaPlay)
        self.pause_icon = control_icon(style, QStyle.StandardPixmap.SP_MediaPause)
        self.play_button = QPushButton()
        self.play_button.setObjectName("playButton")
        self.play_button.clicked.connect(self.toggle_play)
        bar.addWidget(self.play_button)
        self.next_button = QPushButton(control_icon(style, QStyle.StandardPixmap.SP_MediaSkipForward), "")
        self.next_button.setAccessibleName("下一首")
        self.next_button.setToolTip("下一首")
        self.next_button.clicked.connect(lambda: self.play_adjacent(1))
        bar.addWidget(self.next_button)
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setAccessibleName("播放进度")
        self.progress.setToolTip(f"播放进度（← / 短按 → 跳转 {SEEK_MS // 1000} 秒）")
        self.progress.setMinimumWidth(42)
        self.progress.setRange(0, 0)
        self.progress.setEnabled(False)
        self.progress.valueChanged.connect(self.progress_changed)
        self.progress.sliderReleased.connect(self.commit_progress)
        self.progress.sliderPressed.connect(self.controls_hide_timer.stop)
        self.progress.sliderReleased.connect(self.update_fullscreen_controls)
        bar.addWidget(self.progress, 1)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("time")
        bar.addWidget(self.time_label)

        volume = QHBoxLayout()
        volume.setSpacing(4)
        volume_tip = "音量（键盘↑ / ↓ 或鼠标滚轮调节）"
        self.speaker_icon = control_icon(style, QStyle.StandardPixmap.SP_MediaVolume)
        self.muted_icon = control_icon(style, QStyle.StandardPixmap.SP_MediaVolumeMuted)
        self.mute_button = QPushButton()
        self.mute_button.setObjectName("muteButton")
        self.mute_button.setFixedWidth(24)
        self.mute_button.setCheckable(True)
        self.mute_button.toggled.connect(self.audio.setMuted)
        volume.addWidget(self.mute_button)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setAccessibleName("音量")
        self.volume_slider.setToolTip(volume_tip)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setSingleStep(VOLUME_STEP)
        self.volume_slider.setMinimumWidth(50)
        self.volume_slider.setMaximumWidth(76)
        self.volume_slider.valueChanged.connect(self.set_volume)
        self.volume_slider.sliderPressed.connect(self.controls_hide_timer.stop)
        self.volume_slider.sliderReleased.connect(self.update_fullscreen_controls)
        volume.addWidget(self.volume_slider)
        self.volume_label = QLabel()
        self.volume_label.setObjectName("volumeLevel")
        self.volume_label.setToolTip(volume_tip)
        self.volume_label.setMinimumWidth(self.volume_label.fontMetrics().horizontalAdvance("100%"))
        self.volume_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        volume.addWidget(self.volume_label)
        bar.addLayout(volume)

        self.panorama_button = QPushButton("全景")
        self.panorama_button.setCheckable(True)
        self.panorama_button.setToolTip("360° 全景（拖动画面或按 WASD 转向，Q 放大，E 缩小）")
        self.panorama_button.clicked.connect(self.set_panorama_preference)
        bar.addWidget(self.panorama_button)

        self.repeat_button = QPushButton(option_icon(REPEAT_STROKES), "")
        self.repeat_button.setAccessibleName("单曲循环")
        self.repeat_button.setToolTip("单曲循环")
        self.repeat_button.setCheckable(True)
        self.repeat_button.setChecked(self.settings.value("playback/repeatOne", False, type=bool))
        self.repeat_button.toggled.connect(self.set_repeat)
        bar.addWidget(self.repeat_button)
        self.shuffle_button = QPushButton(option_icon(SHUFFLE_STROKES), "")
        self.shuffle_button.setAccessibleName("随机播放")
        self.shuffle_button.setCheckable(True)
        self.shuffle_button.setChecked(self.settings.value("playback/shuffle", False, type=bool))
        self.shuffle_button.toggled.connect(self.set_shuffle)
        bar.addWidget(self.shuffle_button)

        self.rate_button = QPushButton()
        self.rate_button.setObjectName("rateButton")
        self.rate_button.setAccessibleName("播放速度")
        self.rate_button.setToolTip("播放速度（Z 减速，X 加速）")
        self.rate_menu = QMenu(self.rate_button)
        self.rate_menu.aboutToShow.connect(self.controls_hide_timer.stop)
        self.rate_menu.aboutToHide.connect(lambda: QTimer.singleShot(0, self.update_fullscreen_controls))
        rate_choices = QActionGroup(self.rate_menu)
        for rate in PLAYBACK_RATES:
            action = self.rate_menu.addAction(f"{rate:g}×")
            action.setCheckable(True)
            action.setData(rate)
            rate_choices.addAction(action)
            action.setChecked(rate == self.selected_rate)
        rate_choices.triggered.connect(lambda action: self.set_selected_rate(action.data()))
        self.rate_button.setMenu(self.rate_menu)
        bar.addWidget(self.rate_button)

        self.panel_button = QPushButton("列表")
        self.panel_button.setCheckable(True)
        self.panel_button.toggled.connect(self.update_panel_visibility)
        bar.addWidget(self.panel_button)

        self.settings_button = QPushButton(option_icon(SETTINGS_STROKES, ring=True), "")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setToolTip("设置")
        self.settings_button.setAccessibleName("设置")
        self.settings_menu = QMenu(self.settings_button)
        self.settings_menu.aboutToShow.connect(self.show_settings)
        self.settings_menu.aboutToHide.connect(lambda: QTimer.singleShot(0, self.update_fullscreen_controls))
        self.multi_window_action = self.settings_menu.addAction("启用多窗口播放")
        self.multi_window_action.setCheckable(True)
        self.multi_window_action.setChecked(self.settings.value("playback/multiWindow", False, type=bool))
        self.multi_window_action.toggled.connect(self.set_multi_window)
        hold_menu = self.settings_menu.addMenu("长按右键倍速")
        self.hold_rate_choices = QActionGroup(hold_menu)
        for rate in HOLD_RATES:
            action = hold_menu.addAction(f"{rate:g}×")
            action.setCheckable(True)
            action.setData(rate)
            action.setChecked(rate == self.hold_rate)
            self.hold_rate_choices.addAction(action)
        self.hold_rate_choices.triggered.connect(lambda action: self.set_hold_rate(action.data()))
        self.settings_menu.addSeparator()
        self.settings_menu.addAction("快捷键与鼠标操作", self.show_shortcut_help)
        self.settings_button.setMenu(self.settings_menu)
        bar.addWidget(self.settings_button)

        for button in (open_button, self.previous_button, self.play_button, self.next_button,
                       self.repeat_button, self.shuffle_button, self.settings_button):
            button.setProperty("mediaControl", True)
            button.setFixedWidth(30)
        self.controls.setMouseTracking(True)
        for widget in self.controls.findChildren(QWidget):
            widget.setMouseTracking(True)

    def update_panel_visibility(self):
        visible = self.panel_button.isChecked() and not self.isFullScreen()
        self.panel.setVisible(visible)
        if visible:
            available = self.splitter.width() - self.splitter.handleWidth()
            surface_width = max(self.surface.minimumWidth(), available - self.playlist_width)
            self.splitter.setSizes([surface_width, available - surface_width])

    def remember_playlist_width(self, _position, _index):
        self.playlist_width = self.splitter.sizes()[1]

    def open_files(self):
        self.settings.sync()
        directory = self.settings.value("files/lastOpenDirectory", "", type=str)
        if directory and not QDir(directory).exists():
            directory = ""
        paths, _ = QFileDialog.getOpenFileNames(self, "打开媒体文件", directory, filter=MEDIA_FILTER)
        if paths:
            self.settings.setValue("files/lastOpenDirectory", str(Path(paths[0]).absolute().parent))
            self.settings.sync()
            self.load_files(paths)

    def load_files(self, paths: list[str]):
        files = [path.absolute() for name in paths if (path := Path(name)).is_file()]
        if not files:
            return
        self.playlist.replace(files)
        self.track_list.clear()
        self.add_track_rows(files)
        self.play_row(self.playlist.order[0])

    def append_files(self, paths: list[str]):
        files = [path.absolute() for name in paths if (path := Path(name)).is_file()]
        if not files:
            return
        self.playlist.append(files)
        self.add_track_rows(files)
        if self.playlist.current is None:
            self.play_row(self.playlist.order[0])
        else:
            self.advance_after_end()
            self.update_navigation()

    def add_track_rows(self, files: list[Path]):
        for path in files:
            self.track_list.addItem(path.name)
            self.track_list.item(self.track_list.count() - 1).setToolTip(str(path))
        if self.track_list.count() > 1:
            self.panel_button.setChecked(True)

    def nativeEvent(self, event_type, message):
        if playlist_ipc.IS_WINDOWS and event_type == b"windows_generic_MSG":
            request = playlist_ipc.receive_files(int(message))
            if request is not None:
                kind, paths = request
                if paths:
                    open_files = self.replace_from_shell if kind == playlist_ipc.OPEN_MESSAGE else self.append_files
                    QTimer.singleShot(0, self, lambda: open_files(paths))
                    return True, 1
        return super().nativeEvent(event_type, message)

    def replace_from_shell(self, paths: list[str]):
        self.load_files(paths)
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def play_row(self, row: int):
        if not self.fast_playback:
            self.finish_right_gesture()
        self.surface.click_timer.stop()
        if self.surface.panorama_active:
            self.surface.set_panorama(False)
        self.surface.panorama.reset_view()
        self.panorama_unavailable = False
        self.playlist.current = row
        self.track_list.setCurrentRow(row)
        path = self.playlist.files[row]
        self.media_error = ""
        self.setWindowTitle(f"{path.name} · {APP_NAME}")
        source = QUrl.fromLocalFile(str(path))
        if self.player.source() == source:
            self.player.setSource(QUrl())
        self.player.setSource(source)
        self.apply_playback_rate()
        self.update_surface()
        self.update_navigation()
        self.update_play_button()
        self.player.play()

    def play_selected(self, item):
        row = self.track_list.row(item)
        self.playlist.current = row
        self.playlist.set_shuffle(self.playlist.shuffled)
        self.play_row(row)

    def set_shuffle(self, enabled: bool):
        self.settings.setValue("playback/shuffle", enabled)
        self.update_play_modes()

    def set_repeat(self, enabled: bool):
        self.settings.setValue("playback/repeatOne", enabled)
        self.update_play_modes()

    def update_play_modes(self):
        repeat = self.repeat_button.isChecked()
        self.shuffle_button.setEnabled(not repeat)
        self.shuffle_button.setToolTip("随机播放（单曲循环时暂停）" if repeat else "随机播放")
        shuffled = self.shuffle_button.isChecked() and not repeat
        if self.playlist.shuffled != shuffled:
            self.playlist.set_shuffle(shuffled)
        self.update_navigation()

    def play_adjacent(self, offset: int):
        row = self.playlist.adjacent(offset)
        if row is not None:
            self.play_row(row)

    def media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            QTimer.singleShot(0, self, self.advance_after_end)

    def advance_after_end(self):
        if self.player.mediaStatus() != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        if self.repeat_button.isChecked():
            self.player.play()
        else:
            self.play_adjacent(1)

    def update_navigation(self):
        self.previous_button.setEnabled(self.playlist.adjacent(-1) is not None)
        self.next_button.setEnabled(self.playlist.adjacent(1) is not None)

    def update_play_button(self):
        playing = self.player.isPlaying()
        label = "暂停" if playing else "播放"
        self.play_button.setEnabled(self.playlist.current is not None)
        self.play_button.setIcon(self.pause_icon if playing else self.play_icon)
        self.play_button.setAccessibleName(label)
        self.play_button.setToolTip(f"{label}（空格）")

    def toggle_play(self):
        if self.playlist.current is None:
            return
        if self.player.isPlaying():
            self.player.pause()
        else:
            self.player.play()

    def update_surface(self):
        row = self.playlist.current
        has_video = row is not None and self.player.hasVideo() and not self.media_error
        can_show_panorama = has_video and not self.panorama_unavailable and not self.surface.panorama.shutting_down
        active = can_show_panorama and self.panorama_enabled
        self.panorama_button.setChecked(active)
        self.panorama_button.setEnabled(can_show_panorama)
        if self.surface.panorama_active != active:
            self.surface.set_panorama(active)
        if self.media_error:
            text = self.media_error
        elif row is None:
            text = "拖入视频或音乐，也可按 Ctrl+O 打开"
        elif self.player.hasVideo():
            text = ""
        else:
            text = self.playlist.files[row].stem
        self.surface.show_message(text)

    def set_panorama_preference(self, enabled: bool):
        self.panorama_enabled = enabled
        self.settings.setValue("panorama/enabled", enabled)
        self.update_surface()

    def save_panorama_zoom(self, fov: float):
        self.settings.setValue("panorama/fov", fov)

    def handle_panorama_failure(self, message: str):
        if self.surface.panorama_active:
            self.panorama_unavailable = True
            self.update_surface()
            point = self.surface.mapToGlobal(self.surface.rect().center())
            QToolTip.showText(point, message, self.surface)

    def show_media_error(self, _error, message: str):
        if self.playlist.current is not None:
            name = self.playlist.files[self.playlist.current].name
            self.media_error = f"无法播放「{name}」\n{message}"
            self.update_surface()

    def update_duration(self, duration: int):
        with QSignalBlocker(self.progress):
            self.progress.setMaximum(duration)
        self.update_position(self.player.position())

    def update_position(self, position: int):
        if not self.progress.isSliderDown():
            with QSignalBlocker(self.progress):
                self.progress.setValue(position)
            self.update_time(position)

    def update_time(self, position: int):
        self.time_label.setText(f"{format_time(position)} / {format_time(self.player.duration())}")

    def progress_changed(self, position: int):
        self.update_time(position)
        if not self.progress.isSliderDown() and self.player.isSeekable():
            self.player.setPosition(position)

    def commit_progress(self):
        if self.player.isSeekable():
            self.player.setPosition(self.progress.sliderPosition())

    def seek_by(self, offset: int):
        if self.player.isSeekable():
            position = self.player.position() + offset
            self.player.setPosition(max(0, min(position, self.player.duration())))

    def adjust_volume(self, change: int):
        current = round(self.audio.volume() * 100)
        self.set_volume(current + change)

    def set_volume(self, percent: int):
        percent = max(0, min(100, percent))
        self.audio.setVolume(percent / 100)
        self.surface.show_volume(percent)

    def update_volume_display(self, volume: float):
        percent = round(volume * 100)
        with QSignalBlocker(self.volume_slider):
            self.volume_slider.setValue(percent)
        self.volume_label.setText(f"{percent}%")

    def save_volume(self, volume: float):
        self.settings.setValue("playback/volume", round(volume * 100))

    def update_mute_button(self, muted: bool):
        label = "取消静音" if muted else "静音"
        with QSignalBlocker(self.mute_button):
            self.mute_button.setChecked(muted)
        self.mute_button.setIcon(self.muted_icon if muted else self.speaker_icon)
        self.mute_button.setAccessibleName(label)
        self.mute_button.setToolTip(label)

    def handle_volume_wheel(self, event) -> bool:
        if self.panel.isVisible():
            position = self.panel.mapFromGlobal(event.globalPosition().toPoint())
            if self.panel.rect().contains(position):
                return False
        delta = event.angleDelta().y()
        if not delta:
            return False

        self.wheel_delta += delta
        steps = int(self.wheel_delta / WHEEL_NOTCH)
        self.wheel_delta -= steps * WHEEL_NOTCH
        if steps:
            self.adjust_volume(steps * VOLUME_STEP)
        event.accept()
        return True

    def set_selected_rate(self, rate: float):
        self.selected_rate = max(PLAYBACK_RATES[0], min(PLAYBACK_RATES[-1], rate))
        index = PLAYBACK_RATES.index(self.selected_rate)
        self.rate_menu.actions()[index].setChecked(True)
        self.apply_playback_rate()

    def show_settings(self):
        self.controls_hide_timer.stop()
        self.settings.sync()
        with QSignalBlocker(self.multi_window_action):
            self.multi_window_action.setChecked(self.settings.value("playback/multiWindow", False, type=bool))
        self.hold_rate = self.saved_hold_rate()
        self.apply_playback_rate()
        self.hold_rate_choices.actions()[HOLD_RATES.index(self.hold_rate)].setChecked(True)

    def show_shortcut_help(self):
        if self.shortcut_help is None:
            self.shortcut_help = ShortcutHelpDialog(self)
        self.shortcut_help.show()
        self.shortcut_help.raise_()
        self.shortcut_help.activateWindow()

    def set_multi_window(self, enabled: bool):
        self.settings.setValue("playback/multiWindow", enabled)
        self.settings.sync()

    def saved_hold_rate(self) -> float:
        rate = self.settings.value("playback/holdRate", DEFAULT_HOLD_RATE, type=float)
        return rate if rate in HOLD_RATES else DEFAULT_HOLD_RATE

    def set_hold_rate(self, rate: float):
        self.hold_rate = rate
        self.settings.setValue("playback/holdRate", rate)
        self.apply_playback_rate()

    def apply_playback_rate(self):
        rate = self.hold_rate if self.fast_playback else self.selected_rate
        if self.player.playbackRate() != rate:
            self.player.setPlaybackRate(rate)

    def update_rate_display(self):
        rate = self.player.playbackRate()
        self.rate_button.setText(f"{rate:g}×")
        self.surface.show_speed(rate if self.isFullScreen() else 1.0)

    def begin_fast_playback(self):
        if self.right_down and self.playlist.current is not None:
            self.fast_playback = True
            self.apply_playback_rate()

    def finish_right_gesture(self, released=False):
        short_press = self.right_down and self.hold_timer.isActive()
        self.right_down = False
        self.hold_timer.stop()
        if self.fast_playback:
            self.fast_playback = False
            self.apply_playback_rate()
        elif released and short_press:
            self.seek_by(SEEK_MS)

    def position_fullscreen_controls(self):
        root = self.centralWidget()
        height = self.controls.sizeHint().height()
        self.controls.setGeometry(0, root.height() - height, root.width(), height)

    def fullscreen_controls_in_use(self) -> bool:
        return (
            self.progress.isSliderDown() or self.volume_slider.isSliderDown()
            or self.rate_menu.isVisible() or self.settings_menu.isVisible()
        )

    def update_fullscreen_controls(self, global_position=None):
        if not self.isFullScreen():
            return
        root = self.centralWidget()
        point = root.mapFromGlobal(QCursor.pos() if global_position is None else global_position)
        edge_height = max(FULLSCREEN_CONTROLS_EDGE, self.controls.sizeHint().height())
        near_bottom = root.rect().contains(point) and point.y() >= root.height() - edge_height
        if near_bottom or self.fullscreen_controls_in_use():
            self.controls_hide_timer.stop()
            if not self.controls.isVisible():
                self.position_fullscreen_controls()
                self.controls.show()
                self.controls.raise_()
        elif self.controls.isVisible() and not self.controls_hide_timer.isActive():
            self.controls_hide_timer.start()

    def hide_fullscreen_controls(self):
        if self.isFullScreen() and not self.fullscreen_controls_in_use():
            self.controls.hide()

    def toggle_fullscreen(self):
        self.controls_hide_timer.stop()
        self.controls.hide()
        if self.isFullScreen():
            self.root_layout.addWidget(self.controls)
            self.panel_button.setEnabled(True)
            if self.was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
            self.controls.show()
            self.update_panel_visibility()
        else:
            self.was_maximized = self.isMaximized()
            self.root_layout.removeWidget(self.controls)
            self.panel.hide()
            self.panel_button.setEnabled(False)
            self.showFullScreen()
            self.position_fullscreen_controls()
        self.update_rate_display()

    def eventFilter(self, watched, event):
        event_type = event.type()
        if watched is self and event_type == QEvent.Type.WindowDeactivate:
            self.finish_right_gesture()
            self.surface.click_timer.stop()
            self.surface.reset_pointer()
            self.surface.panorama.stop_turning()
        if self.isFullScreen():
            if event_type == QEvent.Type.MouseMove and isinstance(watched, QWidget) and watched.window() is self:
                if watched is not self.surface or not event.buttons() & Qt.MouseButton.LeftButton:
                    self.update_fullscreen_controls(event.globalPosition().toPoint())
            elif event_type == QEvent.Type.Resize and watched is self.centralWidget():
                self.position_fullscreen_controls()
            elif event_type == QEvent.Type.Leave and watched is self:
                self.update_fullscreen_controls()
        if event_type not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.Wheel):
            return False
        if not isinstance(watched, QWidget):
            return False
        window = watched.window()
        if window not in (self, self.rate_menu):
            return False
        if event_type == QEvent.Type.Wheel:
            return window is self and self.handle_volume_wheel(event)

        key = event.key()
        panorama_key = self.surface.panorama_active and key in PANORAMA_KEYS
        if panorama_key and event_type == QEvent.Type.KeyRelease:
            self.surface.panorama.camera_key(event)
            return True
        if window is self.rate_menu and key not in (Qt.Key.Key_Z, Qt.Key.Key_X):
            if key != Qt.Key.Key_Right or not self.right_down:
                return False
        if key == Qt.Key.Key_Right and event_type == QEvent.Type.KeyRelease and self.right_down:
            if not event.isAutoRepeat():
                self.finish_right_gesture(released=True)
            return True
        if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            return False
        if panorama_key:
            self.surface.panorama.camera_key(event)
        elif key == Qt.Key.Key_Escape and self.isFullScreen():
            if event_type == QEvent.Type.KeyPress:
                self.toggle_fullscreen()
        elif key == Qt.Key.Key_Space:
            if event_type == QEvent.Type.KeyPress and not event.isAutoRepeat():
                self.toggle_play()
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            if event_type == QEvent.Type.KeyPress:
                self.adjust_volume(VOLUME_STEP if key == Qt.Key.Key_Up else -VOLUME_STEP)
        elif key in (Qt.Key.Key_Z, Qt.Key.Key_X):
            if event_type == QEvent.Type.KeyPress and not event.isAutoRepeat():
                change = RATE_STEP if key == Qt.Key.Key_X else -RATE_STEP
                self.set_selected_rate(self.selected_rate + change)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            if event_type == QEvent.Type.KeyPress and not event.isAutoRepeat():
                if key == Qt.Key.Key_Left:
                    self.seek_by(-SEEK_MS)
                elif not self.right_down:
                    self.right_down = True
                    self.hold_timer.start()
        else:
            return False
        event.accept()
        return True

    def closeEvent(self, event):
        if self.surface.panorama_active:
            self.surface.set_panorama(False)
        if not self.surface.panorama.shutdown():
            event.ignore()
            return
        self.settings.sync()
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.load_files(paths)
            event.acceptProposedAction()


def format_time(milliseconds: int) -> str:
    minutes, seconds = divmod(max(0, milliseconds) // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def main() -> int:
    arguments = sys.argv[1:]
    batch = playlist_ipc.read_batch(arguments)
    if batch == []:
        return 0
    # Qt composites the OpenGL child through the top-level window's surface.
    # The media clock already paces when new frames are ready to present.
    surface_format = QSurfaceFormat.defaultFormat()
    surface_format.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(surface_format)
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    icon = Path(__file__).with_name("icon-designs") / "ribbon" / "player.ico"
    app.setWindowIcon(QIcon(str(icon)))
    app.setStyle(PlayerStyle("Fusion"))
    app.setStyleSheet(Path(__file__).with_name("player.qss").read_text(encoding="utf-8"))
    settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "EasyPlayer", "config")
    replace = batch is None
    paths = [str(Path(name).absolute()) for name in arguments] if replace else batch
    multi_window = settings.value("playback/multiWindow", False, type=bool)
    reuse_window = playlist_ipc.IS_WINDOWS and bool(paths) and (not replace or not multi_window)
    with playlist_ipc.file_startup() if reuse_window else nullcontext():
        if reuse_window and playlist_ipc.forward_files(paths, replace=replace):
            return 0
        window = PlayerWindow(settings)
        window.show()
        window.load_files(paths)
        if playlist_ipc.IS_WINDOWS:
            playlist_ipc.register_window(int(window.winId()))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
