"""
PCU Hackthon - Simplified Video Stream Service
Uses the consolidated camera processor for all video operations.
"""

import cv2
import threading
import time
import logging
import base64
from datetime import datetime

# Import the consolidated camera processing
from .camera import camera_processor, generate_face_encoding

logger = logging.getLogger(__name__)


class VideoStream:
    """
    Simplified video stream wrapper around the consolidated camera processor.
    """

    def __init__(self):
        self.is_running = False
        self.session_id = None
        self.demo_mode = False
        self.last_analysis_result = None

    def start(self, source=0, session_id=None):
        """Start video capture using consolidated camera processor"""
        if self.is_running:
            logger.warning("VideoStream already running")
            return False

        logger.info(f"🎥 VideoStream.start() called with source={source}, session_id={session_id}")
        
        try:
            # FIXED: Use consolidated camera processor
            success = camera_processor.start(source)
            
            if success:
                self.demo_mode = False
                logger.info("🎥 Real camera started successfully")
            else:
                self.demo_mode = True
                logger.info("🎥 Using demo mode (camera not available)")
            
            self.session_id = session_id
            self.is_running = True
            
            logger.info(f"✅ VideoStream started successfully (demo_mode={self.demo_mode})")
            return True
            
        except Exception as e:
            logger.error(f"❌ VideoStream.start() failed: {e}")
            self.demo_mode = True
            return False

    def stop(self):
        """Stop video capture"""
        try:
            self.is_running = False
            
            # FIXED: Stop consolidated camera processor
            camera_processor.stop()
            
            logger.info("✅ VideoStream stopped successfully")
            
        except Exception as e:
            logger.error(f"❌ Error stopping VideoStream: {e}")

    def get_frame(self):
        """Get current frame from camera processor"""
        try:
            # FIXED: Get frame from consolidated camera processor
            return camera_processor.get_frame()
        except Exception as e:
            logger.error(f"Error getting frame: {e}")
            return None

    def get_analysis(self):
        """Get current analysis results"""
        try:
            # FIXED: Get analysis from consolidated camera processor
            analysis = camera_processor.get_analysis()
            self.last_analysis_result = analysis
            return analysis
        except Exception as e:
            logger.error(f"Error getting analysis: {e}")
            return None

    def get_last_analysis(self):
        """Get the last analysis result"""
        return self.last_analysis_result

    # Compatibility helpers used by engagement/views.py
    def get_latest_analysis(self):
        """Return latest analysis and refresh cached value."""
        return self.get_analysis()

    def get_status(self):
        """Return stream runtime status for health/live endpoints."""
        analysis = self.last_analysis_result or self.get_analysis() or {}
        return {
            'is_running': self.is_running,
            'session_id': self.session_id,
            'demo_mode': analysis.get('demo_mode', self.demo_mode),
            'fps': analysis.get('fps', 0),
            'timestamp': datetime.now().isoformat(),
        }

    def get_frame_base64(self):
        """Return current frame encoded as base64 JPEG."""
        frame = self.get_frame()
        if frame is None:
            return None
        ok, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            return None
        return base64.b64encode(buffer.tobytes()).decode('utf-8')


# Global video stream instance
video_stream = VideoStream()


def get_video_stream():
    """Return singleton stream instance."""
    return video_stream


def start_stream(source=0, session_id=None):
    """Start the singleton stream."""
    if video_stream.is_running:
        return True
    return video_stream.start(source=source, session_id=session_id)


def stop_stream():
    """Stop the singleton stream."""
    if not video_stream.is_running:
        return True
    video_stream.stop()
    return True


def generate_mjpeg_frames():
    """Yield MJPEG frames from the singleton stream."""
    stream = get_video_stream()
    while stream.is_running:
        try:
            frame = stream.get_frame()
            if frame is None:
                time.sleep(0.05)
                continue

            ok, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                time.sleep(0.01)
                continue

            frame_bytes = buffer.tobytes()
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n'
            )
            time.sleep(0.033)
        except GeneratorExit:
            break
        except Exception as e:
            logger.error(f"MJPEG generator error: {e}")
            time.sleep(0.1)
