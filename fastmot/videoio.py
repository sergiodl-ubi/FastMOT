from pathlib import Path
from enum import Enum
from collections import deque
from urllib.parse import urlparse
import subprocess
import threading
import logging
import cv2


LOGGER = logging.getLogger(__name__)
WITH_GSTREAMER = True


class Protocol(Enum):
    IMAGE = 0
    VIDEO = 1
    CSI   = 2
    V4L2  = 3
    RTSP  = 4
    HTTP  = 5


class VideoIO:
    def __init__(self, size, input_uri,
                 output_uri=None,
                 resolution=(1920, 1080),
                 frame_rate=30,
                 buffer_size=10,
                 proc_fps=30):
        """Class for video capturing and output saving.
        Encoding, decoding, and scaling can be accelerated using the GStreamer backend.

        Parameters
        ----------
        size : tuple
            Width and height of each frame to output.
        input_uri : str
            URI to input stream. It could be image sequence (e.g. '%06d.jpg'), video file (e.g. 'file.mp4'),
            MIPI CSI camera (e.g. 'csi://0'), USB/V4L2 camera (e.g. '/dev/video0'),
            RTSP stream (e.g. 'rtsp://<user>:<password>@<ip>:<port>/<path>'),
            or HTTP live stream (e.g. 'http://<user>:<password>@<ip>:<port>/<path>')
        output_uri : str, optionals
            URI to an output video file.
        resolution : tuple, optional
            Original resolution of the input source.
            Useful to set a certain capture mode of a USB/CSI camera.
        frame_rate : int, optional
            Frame rate of the input source.
            Required if frame rate cannot be deduced, e.g. image sequence and/or RTSP.
            Useful to set a certain capture mode of a USB/CSI camera.
        buffer_size : int, optional
            Number of frames to buffer.
            For live sources, a larger buffer drops less frames but increases latency.
        proc_fps : int, optional
            Estimated processing speed that may limit the capture interval `cap_dt`.
            This depends on hardware and processing complexity.
        """
        self.size = size
        self.input_uri = input_uri
        self.output_uri = output_uri
        self.resolution = resolution
        assert frame_rate > 0
        self.frame_rate = frame_rate
        assert buffer_size >= 1
        self.buffer_size = buffer_size
        assert proc_fps > 0
        self.proc_fps = proc_fps

        self.protocol = self._parse_uri(self.input_uri)
        self.is_live = self.protocol != Protocol.IMAGE and self.protocol != Protocol.VIDEO
        if WITH_GSTREAMER:
            line = self._gst_cap_pipeline()
            print("Capture pipeline " + line)
            self.source = cv2.VideoCapture(line, cv2.CAP_GSTREAMER)
        else:
            self.source = cv2.VideoCapture(self.input_uri)

        self.frame_queue = deque([], maxlen=self.buffer_size)
        self.cond = threading.Condition()
        self.exit_event = threading.Event()
        self.cap_thread = threading.Thread(target=self._capture_frames)

        ret, frame = self.source.read()
        if not ret:
            raise RuntimeError('Unable to read video stream')
        self.frame_queue.append(frame)

        width = self.source.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = self.source.get(cv2.CAP_PROP_FRAME_HEIGHT)
        self.cap_fps = self.source.get(cv2.CAP_PROP_FPS)
        self.do_resize = (width, height) != self.size
        if self.cap_fps == 0:
            self.cap_fps = self.frame_rate # fallback to config if unknown
        LOGGER.info('%dx%d stream @ %d FPS', width, height, self.cap_fps)

        if self.output_uri is not None:
            Path(self.output_uri).parent.mkdir(parents=True, exist_ok=True)
            output_fps = 1 / self.cap_dt
            if WITH_GSTREAMER:
                line = self._gst_write_pipeline()
                print("Write pipeline: " + line)
                self.writer = cv2.VideoWriter(line, cv2.CAP_GSTREAMER, 0,
                                              output_fps, self.size, True)
            else:
                fourcc = cv2.VideoWriter_fourcc(*'avc1')
                self.writer = cv2.VideoWriter(self.output_uri, fourcc, output_fps, self.size, True)

    @property
    def cap_dt(self):
        # limit capture interval at processing latency for live sources
        return 1 / min(self.cap_fps, self.proc_fps) if self.is_live else 1 / self.cap_fps

    def start_capture(self):
        """Start capturing from file or device."""
        if not self.source.isOpened():
            self.source.open(self._gst_cap_pipeline(), cv2.CAP_GSTREAMER)
        if not self.cap_thread.is_alive():
            self.cap_thread.start()

    def stop_capture(self):
        """Stop capturing from file or device."""
        with self.cond:
            self.exit_event.set()
            self.cond.notify()
        self.frame_queue.clear()
        self.cap_thread.join()

    def read(self):
        """Reads the next video frame.

        Returns
        -------
        ndarray
            Returns None if there are no more frames.
        """
        with self.cond:
            while len(self.frame_queue) == 0 and not self.exit_event.is_set():
                self.cond.wait()
            if len(self.frame_queue) == 0 and self.exit_event.is_set():
                return None
            frame = self.frame_queue.popleft()
            self.cond.notify()
        if self.do_resize:
            frame = cv2.resize(frame, self.size)
        return frame

    def write(self, frame):
        """Writes the next video frame."""
        assert hasattr(self, 'writer')
        self.writer.write(frame)

    def release(self):
        """Cleans up input and output sources."""
        self.stop_capture()
        if hasattr(self, 'writer'):
            self.writer.release()
        self.source.release()

    def _gst_cap_pipeline(self):
        gst_elements = str(subprocess.check_output('gst-inspect-1.0'))
        if 'nvvidconv' in gst_elements and self.protocol != Protocol.V4L2:
            # format conversion for hardware decoder
            cvt_pipeline = (
                'nvvidconv interpolation-method=5 ! '
                'video/x-raw, width=%d, height=%d, format=BGRx !'
                'videoconvert ! appsink sync=false'
                % self.size
            )
        else:
            cvt_pipeline = (
                'videoscale ! '
                'video/x-raw, width=%d, height=%d !'
                'videoconvert ! appsink sync=false'
                % self.size
            )

        if self.protocol == Protocol.IMAGE:
            pipeline = (
                'multifilesrc location=%s index=1 caps="image/%s,framerate=%d/1" ! decodebin ! '
                % (
                    self.input_uri,
                    self._img_format(self.input_uri),
                    self.frame_rate
                )
            )
        elif self.protocol == Protocol.VIDEO:
            pipeline = 'filesrc location=%s ! decodebin ! ' % self.input_uri
        elif self.protocol == Protocol.CSI:
            if 'nvarguscamerasrc' in gst_elements:
                pipeline = (
                    'nvarguscamerasrc sensor_id=%s ! '
                    'video/x-raw(memory:NVMM), width=%d, height=%d, '
                    'format=NV12, framerate=%d/1 ! '
                    % (
                        self.input_uri[6:],
                        *self.resolution,
                        self.frame_rate
                    )
                )
            else:
                raise RuntimeError('GStreamer CSI plugin not found')
        elif self.protocol == Protocol.V4L2:
            if 'v4l2src' in gst_elements:
                #pipeline = (
                #    'v4l2src device=%s ! '
                #    'video/x-raw, width=%d, height=%d, '
                #    'format=YUY2, framerate=%d/1 ! '
                #    % (
                #        self.input_uri,
                #        *self.resolution,
                #        self.frame_rate
                #    )
                #)
                pipeline = (
                    'v4l2src device=%s ! '
                    'video/x-raw, width=%d, height=%d, '
                    'format=YUY2 ! '
                    % (
                        self.input_uri,
                        *self.resolution
                    )
                )
            else:
                raise RuntimeError('GStreamer V4L2 plugin not found')
        elif self.protocol == Protocol.RTSP:
            pipeline = (
                'rtspsrc location=%s latency=0 ! '
                'capsfilter caps=application/x-rtp,media=video ! decodebin ! ' % self.input_uri
            )
        elif self.protocol == Protocol.HTTP:
            pipeline = 'souphttpsrc location=%s is-live=true ! decodebin ! ' % self.input_uri
        return pipeline + cvt_pipeline

    def _gst_write_pipeline(self):
        gst_elements = str(subprocess.check_output('gst-inspect-1.0'))
        # use hardware encoder if found
        if 'omxh264enc' in gst_elements:
            h264_encoder = 'omxh264enc preset-level=2'
        elif 'x264enc' in gst_elements:
            h264_encoder = 'x264enc pass=4'
        else:
            raise RuntimeError('GStreamer H.264 encoder not found')
#        pipeline = (
#            'appsrc ! autovideoconvert ! %s ! qtmux ! filesink location=%s '
#            % (
#                h264_encoder,
#                self.output_uri
#            )
#        )
        pipeline = 'appsrc ! autovideoconvert ! xvimagesink'
        return pipeline

    def _capture_frames(self):
        while not self.exit_event.is_set():
            ret, frame = self.source.read()
            with self.cond:
                if not ret:
                    self.exit_event.set()
                    self.cond.notify()
                    break
                # keep unprocessed frames in the buffer for file
                if not self.is_live:
                    while (len(self.frame_queue) == self.buffer_size and
                           not self.exit_event.is_set()):
                        self.cond.wait()
                self.frame_queue.append(frame)
                self.cond.notify()

    @staticmethod
    def _parse_uri(uri):
        result = urlparse(uri)
        if result.scheme == 'csi':
            protocol = Protocol.CSI
        elif result.scheme == 'rtsp':
            protocol = Protocol.RTSP
        elif result.scheme == 'http':
            protocol = Protocol.HTTP
        else:
            if '/dev/video' in result.path:
                protocol = Protocol.V4L2
            elif '%' in result.path:
                protocol = Protocol.IMAGE
            else:
                protocol = Protocol.VIDEO
        return protocol

    @staticmethod
    def _img_format(uri):
        img_format = Path(uri).suffix[1:]
        return 'jpeg' if img_format == 'jpg' else img_format
