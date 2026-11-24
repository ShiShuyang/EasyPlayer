from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from math import cos, hypot, isfinite, radians, sin
from struct import pack
from threading import Thread

from PySide6.QtCore import QElapsedTimer, QEvent, QPoint, QRect, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImage, QSurfaceFormat, QVector2D
from PySide6.QtMultimedia import QtVideo, QVideoFrame, QVideoFrameFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLPixelTransferOptions,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from shiboken6 import VoidPtr


DEFAULT_FOV = 90.0
MIN_FOV = 30.0
MAX_FOV = 145.0
ZOOM_STEP = 5.0
TURN_KEYS = frozenset((Qt.Key.Key_W, Qt.Key.Key_A, Qt.Key.Key_S, Qt.Key.Key_D))
PANORAMA_KEYS = TURN_KEYS | {Qt.Key.Key_Q, Qt.Key.Key_E}

GL_COLOR_BUFFER_BIT = 0x4000
GL_FLOAT = 0x1406
GL_MAX_TEXTURE_SIZE = 0x0D33
GL_TRIANGLE_STRIP = 0x0005

YUV_LAYOUTS = {
    QVideoFrameFormat.PixelFormat.Format_NV12: (1, 2),
    QVideoFrameFormat.PixelFormat.Format_YUV420P: (1, 1, 1),
}
TEXTURE_FORMATS = {
    1: (QOpenGLTexture.TextureFormat.R8_UNorm, QOpenGLTexture.PixelFormat.Red),
    2: (QOpenGLTexture.TextureFormat.RG8_UNorm, QOpenGLTexture.PixelFormat.RG),
    4: (QOpenGLTexture.TextureFormat.RGBA8_UNorm, QOpenGLTexture.PixelFormat.RGBA),
}


@dataclass(frozen=True)
class YuvFrame:
    width: int
    height: int
    layout: tuple[int, ...]
    planes: tuple[tuple[bytes, int], ...]
    value_range: tuple[float, ...]
    coefficients: tuple[float, ...]

VERTEX_SHADER = """
attribute vec2 position;
varying vec2 screen;

void main() {
    screen = position;
    gl_Position = vec4(position, 0.0, 1.0);
}
"""

FRAGMENT_SHADER = """
varying vec2 screen;
uniform sampler2D panorama;
uniform sampler2D chromaU;
uniform sampler2D chromaV;
uniform int planeCount;
uniform vec3 yuvRange;
uniform vec4 yuvCoefficients;
uniform vec2 imagePlane;
uniform float sphereOffset;
uniform vec2 yaw;
uniform vec2 pitch;

void main() {
    const float pi = 3.141592653589793;

    // Intersect a camera ray with a unit sphere shifted along the view axis.
    vec3 ray = vec3(screen * imagePlane, 1.0);
    float lengthSquared = dot(ray, ray);
    float offsetSquared = sphereOffset * sphereOffset;
    float distance = (sphereOffset + sqrt(offsetSquared + lengthSquared * (1.0 - offsetSquared)))
                     / lengthSquared;
    vec3 direction = ray * distance - vec3(0.0, 0.0, sphereOffset);
    vec3 tilted = vec3(direction.x,
                       pitch.x * direction.y + pitch.y * direction.z,
                       -pitch.y * direction.y + pitch.x * direction.z);
    vec3 world = vec3(yaw.x * tilted.x + yaw.y * tilted.z,
                      tilted.y,
                      -yaw.y * tilted.x + yaw.x * tilted.z);

    float longitude = atan(world.x, world.z);
    float latitude = asin(clamp(world.y, -1.0, 1.0));
    vec2 uv = vec2(0.5 + longitude / (2.0 * pi), 0.5 - latitude / pi);
    vec3 color = texture2D(panorama, uv).rgb;
    if (planeCount > 1) {
        vec2 chroma = texture2D(chromaU, uv).rg;
        if (planeCount == 3)
            chroma.y = texture2D(chromaV, uv).r;
        chroma = (chroma - 128.0 / 255.0) * yuvRange.z;
        float luma = (color.r - yuvRange.x) * yuvRange.y;
        color = vec3(luma + chroma.y * yuvCoefficients.x,
                     luma - dot(chroma, yuvCoefficients.zw),
                     luma + chroma.x * yuvCoefficients.y);
    }
    gl_FragColor = vec4(color, 1.0);
}
"""


def projection(fov: float) -> tuple[float, float]:
    half_angle = radians(fov / 2)
    blend = max(0.0, min(1.0, (fov / 2 - 42.0) / (70.0 - 42.0)))
    offset = 0.8 * blend * blend * (3.0 - 2.0 * blend)
    # Adjust the focal length with the offset to preserve the requested edge angle.
    return sin(half_angle) / (cos(half_angle) + offset), offset


def yuv_conversion(frame_format: QVideoFrameFormat) -> tuple[tuple[float, ...], tuple[float, ...]]:
    space = frame_format.colorSpace()
    color_spaces = QVideoFrameFormat.ColorSpace
    if space == color_spaces.ColorSpace_Undefined:
        space = color_spaces.ColorSpace_BT709 if frame_format.frameHeight() > 576 else color_spaces.ColorSpace_BT601
    if space == color_spaces.ColorSpace_BT2020:
        red, blue = 0.2627, 0.0593
    elif space == color_spaces.ColorSpace_BT709:
        red, blue = 0.2126, 0.0722
    else:
        red, blue = 0.299, 0.114
    green = 1.0 - red - blue
    red_chroma = 2 * (1 - red)
    blue_chroma = 2 * (1 - blue)
    coefficients = red_chroma, blue_chroma, blue * blue_chroma / green, red * red_chroma / green
    if frame_format.colorRange() == QVideoFrameFormat.ColorRange.ColorRange_Full:
        value_range = (0.0, 1.0, 1.0)
    else:
        value_range = (16 / 255, 255 / 219, 255 / 224)
    return value_range, coefficients


def plane_size(width: int, height: int, plane: int) -> tuple[int, int]:
    return (width // 2, height // 2) if plane else (width, height)


def read_yuv(frame: QVideoFrame, layout: tuple[int, ...], texture_limit: int) -> YuvFrame | None:
    width, height = frame.width(), frame.height()
    frame_format = frame.surfaceFormat()
    if (
        width % 2 or height % 2 or max(width, height) > texture_limit
        or frame_format.viewport() != QRect(QPoint(), frame.size())
        or frame_format.scanLineDirection() != QVideoFrameFormat.Direction.TopToBottom
        or frame_format.rotation() != QtVideo.Rotation.None_ or frame_format.isMirrored()
        or frame.rotation() != QtVideo.Rotation.None_ or frame.mirrored()
        or frame_format.colorTransfer() in (
            QVideoFrameFormat.ColorTransfer.ColorTransfer_ST2084,
            QVideoFrameFormat.ColorTransfer.ColorTransfer_STD_B67,
        )
    ):
        return None
    if not frame.map(QVideoFrame.MapMode.ReadOnly):
        return None
    try:
        if frame.planeCount() != len(layout):
            return None
        planes = []
        for plane, channels in enumerate(layout):
            plane_width, plane_height = plane_size(width, height, plane)
            stride = frame.bytesPerLine(plane)
            row_bytes = plane_width * channels
            required_bytes = stride * (plane_height - 1) + row_bytes
            if stride < row_bytes or stride % channels or frame.mappedBytes(plane) < required_bytes:
                return None
            planes.append((bytes(frame.bits(plane)[:required_bytes]), stride // channels))
        value_range, coefficients = yuv_conversion(frame_format)
        return YuvFrame(width, height, layout, tuple(planes), value_range, coefficients)
    finally:
        frame.unmap()


def prepare_video_frame(frame: QVideoFrame, supports_yuv: bool, texture_limit: int) -> QImage | YuvFrame | QVideoFrame:
    layout = YUV_LAYOUTS.get(frame.pixelFormat())
    if supports_yuv and layout is not None:
        yuv = read_yuv(frame, layout, texture_limit)
        if yuv is not None:
            return yuv

    # Read packed RGB directly. Qt's general conversion can create graphics
    # resources, so leave that fallback to the GUI thread.
    image_format = QVideoFrameFormat.imageFormatFromPixelFormat(frame.pixelFormat())
    frame_format = frame.surfaceFormat()
    if (
        image_format == QImage.Format.Format_Invalid
        or frame_format.viewport() != QRect(QPoint(), frame.size())
        or frame_format.scanLineDirection() != QVideoFrameFormat.Direction.TopToBottom
        or frame_format.rotation() != QtVideo.Rotation.None_ or frame_format.isMirrored()
        or frame.rotation() != QtVideo.Rotation.None_ or frame.mirrored()
        or not frame.map(QVideoFrame.MapMode.ReadOnly)
    ):
        return frame
    try:
        image = QImage(frame.bits(0), frame.width(), frame.height(), frame.bytesPerLine(0), image_format).copy()
    finally:
        frame.unmap()
    return prepare_image(image, texture_limit)


def prepare_image(image: QImage, texture_limit: int) -> QImage:
    if max(image.width(), image.height()) > texture_limit:
        image = image.scaled(texture_limit, texture_limit, Qt.AspectRatioMode.KeepAspectRatio)
    if image.format() not in (QImage.Format.Format_RGB32, QImage.Format.Format_ARGB32):
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return image


class PanoramaView(QOpenGLWidget):
    failed = Signal(str)
    zoom_changed = Signal(float)
    frame_prepared = Signal(int, object)
    reader_stopped = Signal()
    shutdown_finished = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        surface_format = QSurfaceFormat()
        surface_format.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        surface_format.setVersion(2, 1)
        surface_format.setSwapInterval(0)
        self.setFormat(surface_format)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.yaw = 0.0
        self.pitch = 0.0
        self.fov = DEFAULT_FOV
        self.turn_keys = set()
        self.turn_clock = QElapsedTimer()
        self.turn_timer = QTimer(self, interval=16)
        self.turn_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.turn_timer.timeout.connect(self.turn_tick)

        self.executor = None
        self.preparation = None
        self.frame_generation = 0
        self.latest_frame = None
        self.pending_pixels = None
        self.frame_prepared.connect(self.accept_prepared_frame, Qt.ConnectionType.QueuedConnection)
        self.shutting_down = False
        self.reader_stopped.connect(self.finish_shutdown, Qt.ConnectionType.QueuedConnection)
        self.program = None
        self.vertices = None
        self.textures = []
        self.texture_layout = None
        self.yuv_parameters = None

    def reset_view(self):
        self.yaw = 0.0
        self.pitch = 0.0
        self.update()

    def set_fov(self, fov: float):
        fov = max(MIN_FOV, min(MAX_FOV, fov)) if isfinite(fov) else DEFAULT_FOV
        if self.fov != fov:
            self.fov = fov
            self.update()
            self.zoom_changed.emit(fov)

    def rotate(self, yaw: float, pitch: float):
        self.yaw = (self.yaw + yaw + 180.0) % 360.0 - 180.0
        self.pitch = max(-90.0, min(90.0, self.pitch + pitch))
        self.update()

    def drag(self, dx: float, dy: float):
        degrees_per_pixel = self.fov / max(1, self.width(), self.height())
        self.rotate(-dx * degrees_per_pixel, dy * degrees_per_pixel)

    def camera_key(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Q, Qt.Key.Key_E):
            if event.type() == QEvent.Type.KeyPress:
                change = -ZOOM_STEP if key == Qt.Key.Key_Q else ZOOM_STEP
                self.set_fov(self.fov + change)
            return
        if event.isAutoRepeat():
            return
        if event.type() == QEvent.Type.KeyPress:
            self.turn_keys.add(key)
            if not self.turn_timer.isActive():
                self.turn_clock.start()
                self.turn(0.035)
                self.turn_timer.start()
        elif event.type() == QEvent.Type.KeyRelease:
            self.turn_keys.discard(key)
            if not self.turn_keys:
                self.turn_timer.stop()

    def turn(self, seconds: float):
        horizontal = (Qt.Key.Key_D in self.turn_keys) - (Qt.Key.Key_A in self.turn_keys)
        vertical = (Qt.Key.Key_W in self.turn_keys) - (Qt.Key.Key_S in self.turn_keys)
        if horizontal or vertical:
            step = seconds * self.fov / hypot(horizontal, vertical)
            self.rotate(horizontal * step, vertical * step)

    def turn_tick(self):
        self.turn(min(self.turn_clock.restart() / 1000, 0.05))

    def stop_turning(self):
        self.turn_keys.clear()
        self.turn_timer.stop()

    def submit_frame(self, frame: QVideoFrame):
        if frame.isValid():
            self.latest_frame = QVideoFrame(frame)
            self.prepare_latest_frame()

    def prepare_latest_frame(self):
        if self.shutting_down or self.preparation is not None or self.latest_frame is None or self.program is None:
            return
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1)
        generation = self.frame_generation
        frame = self.latest_frame
        self.latest_frame = None
        # A decoder readback may block. Keep the UI free to pan using the last frame.
        self.preparation = self.executor.submit(prepare_video_frame, frame, self.supports_yuv, self.texture_limit)
        self.preparation.add_done_callback(lambda future: self.frame_prepared.emit(generation, future))

    @Slot(int, object)
    def accept_prepared_frame(self, generation, future):
        if future is not self.preparation:
            return
        self.preparation = None
        if generation == self.frame_generation:
            try:
                self.pending_pixels = future.result()
            except (RuntimeError, ValueError):
                self.failed.emit("无法准备全景视频帧")
            else:
                if isinstance(self.pending_pixels, QVideoFrame):
                    self.pending_pixels = prepare_image(self.pending_pixels.toImage(), self.texture_limit)
                self.update()
        self.prepare_latest_frame()

    def check_context(self):
        if self.isVisible() and (not self.isValid() or self.program is None):
            self.failed.emit("无法创建 OpenGL 全景画面")

    def initializeGL(self):
        self.context().aboutToBeDestroyed.connect(self.release_gl)
        self.gl = self.context().functions()
        self.gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        self.texture_limit = self.gl.glGetIntegerv(GL_MAX_TEXTURE_SIZE)
        self.supports_yuv = (
            self.context().format().majorVersion() >= 3
            or self.context().hasExtension(b"GL_ARB_texture_rg")
        )

        program = QOpenGLShaderProgram()
        program.bindAttributeLocation("position", 0)
        if not (
            program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VERTEX_SHADER)
            and program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT_SHADER)
            and program.link()
        ):
            self.failed.emit("无法初始化全景投影")
            return

        self.vertices = QOpenGLBuffer()
        if not self.vertices.create() or not self.vertices.bind():
            self.failed.emit("无法创建全景渲染缓冲区")
            return
        corners = pack("8f", -1, -1, 1, -1, -1, 1, 1, 1)
        self.vertices.allocate(corners, len(corners))
        self.vertices.release()
        self.uniforms = {
            name: program.uniformLocation(name)
            for name in (
                "panorama", "chromaU", "chromaV", "planeCount", "yuvRange", "yuvCoefficients",
                "imagePlane", "sphereOffset", "yaw", "pitch",
            )
        }
        self.program = program
        self.prepare_latest_frame()

    def paintGL(self):
        self.gl.glClear(GL_COLOR_BUFFER_BIT)
        if self.program is None:
            return
        if self.pending_pixels is not None:
            pixels = self.pending_pixels
            self.pending_pixels = None
            self.upload_frame(pixels)
        if not self.textures:
            return

        scale, offset = projection(self.fov)
        longest_side = max(1, self.width(), self.height())
        yaw, pitch = radians(self.yaw), radians(self.pitch)
        self.program.bind()
        for unit, texture in enumerate(self.textures):
            texture.bind(unit)
        self.gl.glUniform1i(self.uniforms["panorama"], 0)
        self.gl.glUniform1i(self.uniforms["chromaU"], 1)
        self.gl.glUniform1i(self.uniforms["chromaV"], 2)
        self.gl.glUniform1i(self.uniforms["planeCount"], len(self.textures))
        if self.yuv_parameters is not None:
            value_range, coefficients = self.yuv_parameters
            self.gl.glUniform3f(self.uniforms["yuvRange"], *value_range)
            self.gl.glUniform4f(self.uniforms["yuvCoefficients"], *coefficients)
        self.program.setUniformValue(
            self.uniforms["imagePlane"],
            QVector2D(scale * self.width() / longest_side, scale * self.height() / longest_side),
        )
        self.gl.glUniform1f(self.uniforms["sphereOffset"], offset)
        self.program.setUniformValue(self.uniforms["yaw"], QVector2D(cos(yaw), sin(yaw)))
        self.program.setUniformValue(self.uniforms["pitch"], QVector2D(cos(pitch), sin(pitch)))
        self.vertices.bind()
        self.program.enableAttributeArray(0)
        self.program.setAttributeBuffer(0, GL_FLOAT, 0, 2)
        self.gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        self.program.disableAttributeArray(0)
        self.vertices.release()
        for unit, texture in enumerate(self.textures):
            texture.release(unit)
        self.program.release()

    def upload_frame(self, image: QImage | YuvFrame):
        if isinstance(image, YuvFrame):
            if not self.prepare_textures(image.width, image.height, image.layout):
                self.failed.emit("无法分配全景视频纹理")
                return
            for plane, (data, row_length) in enumerate(image.planes):
                pixel_format = TEXTURE_FORMATS[image.layout[plane]][1]
                self.upload_pixels(plane, pixel_format, data, row_length)
            self.yuv_parameters = image.value_range, image.coefficients
            return
        if image.isNull():
            if not self.textures:
                self.failed.emit("无法读取全景视频帧")
            return
        if image.format() in (QImage.Format.Format_RGB32, QImage.Format.Format_ARGB32):
            pixel_format = QOpenGLTexture.PixelFormat.BGRA
        else:
            pixel_format = QOpenGLTexture.PixelFormat.RGBA

        if not self.prepare_textures(image.width(), image.height(), (4,)):
            self.failed.emit("无法分配全景视频纹理")
            return
        self.yuv_parameters = None
        self.upload_pixels(0, pixel_format, image.constBits(), image.bytesPerLine() // 4)

    def prepare_textures(self, width: int, height: int, layout: tuple[int, ...]) -> bool:
        key = (width, height, layout)
        if self.texture_layout == key:
            return True
        self.destroy_textures()
        for plane, channels in enumerate(layout):
            texture_format, pixel_format = TEXTURE_FORMATS[channels]
            texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            texture.setFormat(texture_format)
            texture.setSize(*plane_size(width, height, plane))
            texture.setMipLevels(1)
            texture.allocateStorage(pixel_format, QOpenGLTexture.PixelType.UInt8)
            if not texture.isStorageAllocated():
                self.destroy_textures()
                return False
            texture.setMinMagFilters(QOpenGLTexture.Filter.Linear, QOpenGLTexture.Filter.Linear)
            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            texture.setWrapMode(QOpenGLTexture.CoordinateDirection.DirectionS, QOpenGLTexture.WrapMode.Repeat)
            self.textures.append(texture)
        self.texture_layout = key
        return True

    def upload_pixels(self, plane, pixel_format, data, row_length):
        transfer = QOpenGLPixelTransferOptions()
        transfer.setAlignment(1)
        transfer.setRowLength(row_length)
        self.textures[plane].setData(pixel_format, QOpenGLTexture.PixelType.UInt8, VoidPtr(data), transfer)

    def destroy_textures(self):
        for texture in self.textures:
            texture.destroy()
        self.textures.clear()
        self.texture_layout = None
        self.yuv_parameters = None

    def suspend(self):
        self.stop_turning()
        self.frame_generation += 1
        self.latest_frame = None
        self.pending_pixels = None
        if self.textures and self.isValid():
            self.makeCurrent()
            self.destroy_textures()
            self.doneCurrent()
        self.hide()

    def shutdown(self) -> bool:
        if self.shutting_down:
            return False
        self.suspend()
        self.release_gl()
        if self.executor is None:
            return True

        executor = self.executor
        self.executor = None
        self.preparation = None
        self.shutting_down = True

        def stop_reader():
            # Qt may need to run GUI events while releasing the reader's graphics resources.
            executor.shutdown(wait=True)
            self.reader_stopped.emit()

        Thread(target=stop_reader, daemon=True).start()
        return False

    @Slot()
    def finish_shutdown(self):
        self.shutting_down = False
        self.shutdown_finished.emit()

    def release_gl(self):
        if self.program is None and self.vertices is None:
            return
        self.makeCurrent()
        self.destroy_textures()
        if self.vertices is not None:
            self.vertices.destroy()
            self.vertices = None
        self.program = None
        self.doneCurrent()
