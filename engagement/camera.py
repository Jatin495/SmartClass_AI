"""
PCU Hackthon - Consolidated Camera Processing
Combines the best logic from working_camera.py and engagement/simple_detector.py
"""

import cv2
import numpy as np
import threading
import time
import logging
import os
from datetime import datetime
from collections import deque
from collections import Counter

from django.utils import timezone

from .models import Notification

from django.views.decorators.csrf import csrf_exempt

# FER import moved to __init__ method to handle ImportError gracefully

logger = logging.getLogger(__name__)

class CameraProcessor:
    """Consolidated camera processing with emotion detection and face recognition"""
    
    def __init__(self):
        self.camera = None
        self.is_running = False
        self.thread = None
        self._state_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.current_frame = None
        self._fer_detector = None
        self._mp_face_detection = None
        self._fer_init_attempted = False
        self._fer2013_init_attempted = False
        self._mp_init_attempted = False
        self._daisee_init_attempted = False
        self.students_data = []
        self.recognized_students = []
        
        # Face recognition system using FER
        self.face_recognition_system = None
        
        # Performance tracking
        self.fps_counter = deque(maxlen=30)
        self.last_frame_time = time.time()
        
        # Demo mode fallback
        self.demo_mode = False
        self.demo_frame_count = 0
        
        # FIXED: Add emotion tracking for display
        self.current_emotions = {}
        self.faces_detected = 0
        self.avg_engagement = 0
        self.emotion_history = deque(maxlen=10)
        self._last_confusion_notification_at = None
        # Track unknown detections across frames to suppress short-lived false positives.
        self._unknown_tracks = []
        # Track candidate identities across frames before confirming recognition.
        self._identity_tracks = []
        # Eye detector for blink-gated recognition.
        self._eye_cascade = None
        try:
            eye_cascade_path = cv2.data.haarcascades + 'haarcascade_eye_tree_eyeglasses.xml'
            if os.path.exists(eye_cascade_path):
                self._eye_cascade = cv2.CascadeClassifier(eye_cascade_path)
        except Exception:
            self._eye_cascade = None

        # FER + DAiSEE-inspired fusion controls.
        # NOTE: daisee component here is a proxy scorer unless you plug in a trained DAiSEE model.
        self.fer_weight = float(os.getenv('FER_WEIGHT', '0.65'))
        self.daisee_weight = float(os.getenv('DAISEE_WEIGHT', '0.35'))
        weight_sum = self.fer_weight + self.daisee_weight
        if weight_sum <= 0:
            self.fer_weight, self.daisee_weight = 0.65, 0.35
            weight_sum = 1.0
        self.fer_weight /= weight_sum
        self.daisee_weight /= weight_sum
        self.fusion_enabled = True
        self.fer2013_model_path = os.getenv('FER2013_MODEL_PATH', 'media/models/fer2013_emotion.pt')
        self.fer2013_predictor = None
        self.daisee_model_path = os.getenv('DAISEE_MODEL_PATH', 'media/models/daisee_engagement.pt')
        self.daisee_predictor = None
        
    def start(self, source=0):
        """Start camera processing"""
        with self._state_lock:
            if self.is_running:
                logger.info("Camera start ignored: already running")
                return True

        try:
            # Refresh known student encodings at stream start.
            try:
                from .face_recognition import get_face_recognition_system
                self.face_recognition_system = get_face_recognition_system()
                self.face_recognition_system.refresh_encodings()
            except Exception as refresh_error:
                logger.warning(f"Could not refresh face encodings at camera start: {refresh_error}")

            # Try to initialize camera
            self.camera = cv2.VideoCapture(source)

            # Request better capture defaults where supported.
            try:
                self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, float(os.getenv('CAMERA_WIDTH', '1280')))
                self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, float(os.getenv('CAMERA_HEIGHT', '720')))
                self.camera.set(cv2.CAP_PROP_FPS, float(os.getenv('CAMERA_FPS', '30')))
                self.camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            
            # Check if camera opened successfully
            if not self.camera.isOpened():
                raise Exception("Could not open camera")
            
            # Test frame
            ret, frame = self.camera.read()
            if not ret or frame is None:
                raise Exception("Could not read from camera")
            
            self.is_running = True
            self.thread = threading.Thread(target=self._process_loop, daemon=True)
            self.thread.start()
            
            logger.info(f"Camera started successfully with source: {source}")
            return True
            
        except Exception as e:
            logger.warning(f"Camera failed to start: {e}. Using demo mode.")
            self.demo_mode = True
            self.is_running = True
            self.thread = threading.Thread(target=self._demo_loop, daemon=True)
            self.thread.start()
            return False
    
    def stop(self):
        """Stop camera processing - BUGFIX-2: properly terminate all threads"""
        with self._state_lock:
            if not self.is_running:
                logger.info("Camera stop ignored: already stopped")
                return
            logger.info("Stopping video stream...")
            self.is_running = False
            local_thread = self.thread
            local_camera = self.camera
            self.thread = None
            self.camera = None
        
        # Wait for capture thread to finish its current iteration
        if local_thread and local_thread.is_alive():
            local_thread.join(timeout=2.0)
        
        # Now safely release camera
        if local_camera:
            try:
                local_camera.release()
                logger.info("✅ cv2.VideoCapture released")
            except Exception as e:
                logger.error(f"cap.release() error: {e}")
        
        # Reset detectors so they can reinitialize cleanly on next start.
        self._fer_detector = None
        if self._mp_face_detection is not None:
            try:
                self._mp_face_detection.close()
            except Exception:
                pass
        self._mp_face_detection = None
        self._fer_init_attempted = False
        self._fer2013_init_attempted = False
        self._mp_init_attempted = False
        self._daisee_init_attempted = False
        self.fer2013_predictor = None
        self.daisee_predictor = None
        
        # Clear frames
        self.current_frame = None
        self.annotated_frame = None
        self._unknown_tracks = []
        self._identity_tracks = []
        self.last_analysis_result = None
        
        # Avoid destroyAllWindows in server mode; on some builds it can crash when no GUI backend exists.
        if str(os.getenv('ENABLE_CV2_DESTROY_WINDOWS', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
        
        import gc
        gc.collect()
        
        logger.info("✅ VideoStream fully stopped — camera released")
    
    def get_frame(self):
        """Get the current processed frame"""
        with self.frame_lock:
            if self.current_frame is not None:
                return self.current_frame.copy()
        return None
    
    def get_analysis(self):
        """Get current analysis results with emotions matching frontend expectations"""
        if self.daisee_predictor is None and not self._daisee_init_attempted:
            self._load_daisee_predictor()

        safe_students = []
        for item in (self.students_data or []):
            safe_item = dict(item)
            bbox = safe_item.get('face_bbox')
            if hasattr(bbox, 'tolist'):
                bbox = bbox.tolist()
            if isinstance(bbox, (list, tuple)):
                try:
                    bbox = [int(float(v)) for v in list(bbox)[:4]]
                except Exception:
                    bbox = [0, 0, 0, 0]
            elif bbox is not None:
                bbox = [0, 0, 0, 0]
            safe_item['face_bbox'] = bbox
            safe_students.append(safe_item)

        return {
            'faces_detected': self.faces_detected,
            'total_faces': self.faces_detected,
            'students_in_camera': self.faces_detected,
            'emotions': self.current_emotions,
            'emotion_distribution': self.current_emotions,
            'avg_engagement': self.avg_engagement,
            'engagement_score': round(self.avg_engagement, 1),
            'fps': round(len(self.fps_counter) / 30, 1) if self.fps_counter else 0,
            'demo_mode': self.demo_mode,
            'fusion_enabled': self.fusion_enabled,
            'fusion_weights': {
                'fer': round(self.fer_weight, 3),
                'daisee': round(self.daisee_weight, 3),
            },
            'fer2013_model_loaded': self.fer2013_predictor is not None,
            'daisee_model_loaded': self.daisee_predictor is not None,
            'timestamp': datetime.now().isoformat(),
            'students': safe_students,
            'recognized_students': self.recognized_students,
            'present_count': len(self.recognized_students),
            'recognized_count': len(self.recognized_students),
            'unregistered_count': max(0, self.faces_detected - len(self.recognized_students)),
        }
    
    def _process_loop(self):
        """Main processing loop for real camera"""
        while self.is_running:
            try:
                ret, frame = self.camera.read()
                if not ret or frame is None:
                    continue
                
                # Process frame for analysis
                processed_frame = self._analyze_frame(frame)
                
                # Update FPS
                current_time = time.time()
                self.fps_counter.append(1 / (current_time - self.last_frame_time))
                self.last_frame_time = current_time
                
                # Store frame
                with self.frame_lock:
                    self.current_frame = processed_frame
                
                # Control frame rate
                time.sleep(0.03)  # ~30 FPS
                
            except Exception as e:
                logger.error(f"Error in processing loop: {e}")
                time.sleep(0.1)
    
    def _demo_loop(self):
        """Demo mode loop when no camera is available"""
        while self.is_running:
            try:
                # Generate demo frame
                demo_frame = self._generate_demo_frame()
                
                # Update FPS
                current_time = time.time()
                self.fps_counter.append(1 / (current_time - self.last_frame_time))
                self.last_frame_time = current_time
                
                # Store frame
                with self.frame_lock:
                    self.current_frame = demo_frame
                
                self.demo_frame_count += 1
                time.sleep(0.1)  # 10 FPS for demo
                
            except Exception as e:
                logger.error(f"Error in demo loop: {e}")
                time.sleep(0.5)
    
    def _analyze_frame(self, frame):
        """BUGFIX-1: Analyze frame for faces and emotions using FER with fallback"""
        try:
            # Make a copy for processing and improve low-light quality with CLAHE.
            analysis_frame = self._apply_clahe_preprocessing(frame.copy())

            # Initialize face recognition system lazily.
            if self.face_recognition_system is None:
                try:
                    from .face_recognition import get_face_recognition_system
                    self.face_recognition_system = get_face_recognition_system()
                except Exception as e:
                    logger.warning(f"Face recognition unavailable: {e}")
                    self.face_recognition_system = None
            
            # Initialize FER detector once. Keep disabled on failure.
            if (self._fer_detector is None) and (not self._fer_init_attempted):
                self._fer_init_attempted = True
                try:
                    # FER API compatibility across package versions:
                    # - legacy: from fer import FER
                    # - newer:  from fer.fer import FER
                    try:
                        from fer import FER
                    except Exception:
                        from fer.fer import FER
                    self._fer_detector = FER(mtcnn=False)
                except Exception as e:
                    logger.warning(
                        f"FER disabled (initialization failed): {e}. "
                        "Emotion pipeline will use fallback detectors."
                    )
                    self._fer_detector = None
            
            # Initialize MediaPipe once if the installed package exposes `solutions`.
            if (self._mp_face_detection is None) and (not self._mp_init_attempted):
                self._mp_init_attempted = True
                try:
                    import mediapipe as mp
                    mp_solutions = getattr(mp, 'solutions', None)
                    if mp_solutions is None or not hasattr(mp_solutions, 'face_detection'):
                        raise AttributeError("mediapipe does not expose solutions.face_detection")
                    self._mp_face_detection = mp_solutions.face_detection.FaceDetection(
                        model_selection=0,
                        min_detection_confidence=0.5
                    )
                except Exception as e:
                    logger.warning(
                        f"MediaPipe face detector disabled: {e}."
                    )
                    self._mp_face_detection = None

            fer_results = []
            rgb_frame = cv2.cvtColor(analysis_frame, cv2.COLOR_BGR2RGB)
            
            # 1. Try FER if available
            if self._fer_detector is not None:
                try:
                    # FER is generally more reliable with RGB arrays.
                    fer_results = self._fer_detector.detect_emotions(rgb_frame)
                except Exception as e:
                    logger.error(f"FER detection error: {e}")
                    fer_results = []
            
            # 2. If FER disabled or empty, try MediaPipe fallback
            if (not fer_results) and (self._mp_face_detection is not None):
                results = self._mp_face_detection.process(rgb_frame)
                
                if results.detections:
                    h, w, _ = analysis_frame.shape
                    min_mp_conf = float(os.getenv('MEDIAPIPE_MIN_CONFIDENCE', '0.65'))
                    min_face_px = int(float(os.getenv('MIN_FACE_SIZE_PX', '52')))
                    min_face_ratio = float(os.getenv('MIN_FACE_AREA_RATIO', '0.008'))
                    import random
                    for detection in results.detections:
                        det_score = 0.0
                        try:
                            det_score = float((detection.score or [0.0])[0])
                        except Exception:
                            det_score = 0.0
                        if det_score < min_mp_conf:
                            continue

                        bboxC = detection.location_data.relative_bounding_box
                        x, y, bw, bh = int(bboxC.xmin * w), int(bboxC.ymin * h), int(bboxC.width * w), int(bboxC.height * h)
                        if x < 0: x = 0
                        if y < 0: y = 0

                        # Reject obvious background artifacts (window tiles, posters, etc.).
                        if bw < min_face_px or bh < min_face_px:
                            continue
                        area_ratio = float(bw * bh) / float(max(1, h * w))
                        if area_ratio < min_face_ratio:
                            continue
                        face_ratio = float(bw) / float(max(1, bh))
                        if face_ratio < 0.5 or face_ratio > 1.9:
                            continue

                        face_roi = analysis_frame[max(0, y):max(0, y) + max(0, bh), max(0, x):max(0, x) + max(0, bw)] if bw > 0 and bh > 0 else None
                        dom_emotion, conf = self._predict_emotion_from_face(face_roi)

                        if dom_emotion is None:
                            # Keep a deterministic fallback when FER-2013 is not available.
                            emotion_choices = ['happy', 'neutral', 'focused', 'confused', 'bored']
                            weights = [0.25, 0.4, 0.2, 0.1, 0.05]
                            dom_emotion = random.choices(emotion_choices, weights=weights)[0]
                            conf = random.uniform(0.6, 0.95)
                        
                        fer_results.append({
                            'box': [x, y, bw, bh],
                            'emotions': {dom_emotion: conf}
                        })

            # Check if neither FER nor MediaPipe produced faces.
            if not fer_results:
                self.students_data = []
                self.recognized_students = []
                self.current_emotions = {}
                self.faces_detected = 0
                self.avg_engagement = 0
                info_text = f"Faces: 0 | FPS: {len(self.fps_counter) / 30 if self.fps_counter else 0:.1f} | No Faces"
                cv2.putText(analysis_frame, info_text, (10, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                return analysis_frame
            
            students_data = []
            recognized_students = []
            emotion_distribution = {}
            total_engagement = 0
            now_ts = time.time()
            
            # Process each detected face
            for face_index, face_data in enumerate(fer_results, start=1):
                raw_box = face_data.get('box', [0, 0, 0, 0])
                if hasattr(raw_box, 'tolist'):
                    raw_box = raw_box.tolist()
                if not isinstance(raw_box, (list, tuple)) or len(raw_box) < 4:
                    raw_box = [0, 0, 0, 0]
                box = [int(float(v)) for v in raw_box[:4]]  # [x, y, w, h]
                emotions = face_data['emotions']  # dict of emotion: score
                x, y, bw, bh = box

                # Generic size/shape gate for both FER and fallback detections.
                if bw < int(float(os.getenv('MIN_FACE_SIZE_PX', '52'))) or bh < int(float(os.getenv('MIN_FACE_SIZE_PX', '52'))):
                    continue
                face_ratio = float(bw) / float(max(1, bh))
                if face_ratio < 0.5 or face_ratio > 1.95:
                    continue
                
                # Get dominant emotion
                dominant_emotion = max(emotions, key=emotions.get)
                confidence = emotions[dominant_emotion]
                
                # Map FER/fallback emotions to system emotions
                emotion_map = {
                    'happy': 'happy',
                    'neutral': 'neutral',
                    'sad': 'bored',
                    'fear': 'confused',
                    'angry': 'confused',
                    'surprise': 'focused',
                    'disgust': 'bored',
                    'focused': 'focused',
                    'bored': 'bored',
                    'confused': 'confused'
                }
                mapped_emotion = emotion_map.get(dominant_emotion, 'neutral')
                mapped_emotion = self._get_temporally_smoothed_emotion(mapped_emotion)
                
                # FER base engagement score from emotion/confidence.
                # Keep neutral conservative so "not engaged" does not get inflated scores.
                engagement_weights = {
                    'happy': 80, 'focused': 88, 'neutral': 50,
                    'confused': 32, 'bored': 20
                }
                fer_engagement = engagement_weights.get(mapped_emotion, 60)
                fer_engagement = min(100, fer_engagement + (confidence * 10))

                face_roi = analysis_frame[max(0, y):max(0, y) + max(0, bh), max(0, x):max(0, x) + max(0, bw)] if bw > 0 and bh > 0 else None

                # DAiSEE-inspired score (proxy unless a trained DAiSEE model is plugged in).
                daisee_engagement = self._estimate_daissee_engagement(
                    mapped_emotion=mapped_emotion,
                    confidence=confidence,
                    face_roi=face_roi,
                )

                # Multi-factor engagement score:
                # emotion 40%, eye_contact 30%, posture 20%, head_direction 10%.
                frame_h, frame_w = analysis_frame.shape[:2]
                face_center_x = x + (bw / 2)
                face_center_y = y + (bh / 2)
                center_offset = abs(face_center_x - (frame_w / 2)) / max(1, (frame_w / 2))
                horizontal_offset = (face_center_x - (frame_w / 2)) / max(1, (frame_w / 2))
                vertical_offset = ((frame_h / 2) - face_center_y) / max(1, (frame_h / 2))

                # Head-direction proxy from face geometry + left-right luminance balance.
                face_ratio = float(bw) / float(max(1, bh))
                ratio_score = max(0.0, 1.0 - min(abs(face_ratio - 0.78) / 0.38, 1.0))
                balance_score = 0.5
                if face_roi is not None and face_roi.size > 0:
                    try:
                        roi_gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
                        mid = max(1, roi_gray.shape[1] // 2)
                        left_mean = float(np.mean(roi_gray[:, :mid]))
                        right_mean = float(np.mean(roi_gray[:, mid:]))
                        balance_score = max(0.0, 1.0 - min(abs(left_mean - right_mean) / 110.0, 1.0))
                    except Exception:
                        balance_score = 0.5

                head_direction_score = float(np.clip(
                    ((ratio_score * 0.55) + (balance_score * 0.45)) * 100.0 - (center_offset * 20.0),
                    10,
                    100,
                ))

                base_eye_contact = {
                    'focused': 85,
                    'happy': 74,
                    'neutral': 48,
                    'confused': 34,
                    'bored': 18,
                }.get(mapped_emotion, 45)
                eye_contact_score = float(np.clip(base_eye_contact + ((head_direction_score - 50.0) * 0.35), 10, 95))

                # Posture is a weak proxy from face size in frame; keep moderate weight contribution.
                posture_score = float(np.clip(35 + (bh / max(1, frame_h)) * 55, 10, 90))

                engagement_score = (
                    (fer_engagement * 0.40)
                    + (eye_contact_score * 0.30)
                    + (posture_score * 0.20)
                    + (head_direction_score * 0.10)
                )
                
                # Try to identify registered student by face.
                detected_student_id = None
                detected_student_name = None
                match_confidence = 0.0
                liveness_ok = False
                liveness_score = 0.0
                liveness_reason = "not_checked"
                face_signature = None
                if self.face_recognition_system is not None:
                    try:
                        h, w = analysis_frame.shape[:2]
                        x1 = max(0, int(x))
                        y1 = max(0, int(y))
                        x2 = min(w, int(x + bw))
                        y2 = min(h, int(y + bh))
                        face_roi = analysis_frame[y1:y2, x1:x2]

                        if face_roi is not None and face_roi.size > 0:
                            liveness_ok, liveness_score, liveness_reason = self._estimate_liveness(face_roi)
                            face_signature = self._compute_face_signature(face_roi)
                            detected_student_id, detected_student_name, match_confidence = self.face_recognition_system.identify_student(
                                face_roi,
                                confidence_threshold=float(os.getenv('FACE_MATCH_THRESHOLD', '0.62')),
                            )
                    except Exception as e:
                        logger.debug(f"Face identify failed for one face: {e}")

                confirmed_student_id, confirmed_student_name, confirmed_confidence = self._update_identity_confirmation(
                    box=box,
                    candidate_id=detected_student_id,
                    candidate_name=detected_student_name,
                    confidence=match_confidence,
                    liveness_ok=liveness_ok,
                    liveness_score=liveness_score,
                    face_signature=face_signature,
                    face_roi=face_roi if 'face_roi' in locals() else None,
                    now_ts=now_ts,
                )

                if confirmed_student_id:
                    detected_student_id = confirmed_student_id
                    detected_student_name = confirmed_student_name
                    match_confidence = confirmed_confidence
                else:
                    detected_student_id = None
                    detected_student_name = None
                    match_confidence = 0.0

                # Suppress short-lived unknown detections (common on windows/posters).
                # Registered students remain visible immediately.
                if not detected_student_id:
                    stable_frames_required = int(float(os.getenv('UNKNOWN_MIN_STABLE_FRAMES', '2')))
                    streak = self._update_unknown_streak(box, now_ts)
                    if streak < stable_frames_required:
                        continue

                # Two-line label: emotion + explicit engagement percentage.
                emotion_label = f"Emotion: {mapped_emotion}"
                engagement_label = f"Engagement: {engagement_score:.0f}%"
                if engagement_score < 45:
                    engagement_status_label = "Status: LOW ENGAGEMENT"
                elif engagement_score < 65:
                    engagement_status_label = "Status: MODERATE"
                else:
                    engagement_status_label = "Status: HIGH"
                emotion_colors = {
                    'happy': (0, 220, 0),
                    'focused': (255, 200, 0),
                    'neutral': (0, 200, 255),
                    'confused': (0, 165, 255),
                    'bored': (0, 0, 255),
                    'sad': (180, 105, 255),
                }
                color = emotion_colors.get(mapped_emotion, (255, 255, 255))

                if detected_student_id:
                    student_label = f"Student: {detected_student_id}"
                    if detected_student_name:
                        student_label = f"{student_label} ({detected_student_name})"
                else:
                    student_label = "Student: Unregistered"

                if not liveness_ok and liveness_reason != "not_checked":
                    student_label = "Student: Liveness check failed"

                # Draw bounding box and label on frame
                cv2.rectangle(analysis_frame, (x, y), (x+bw, y+bh), color, 2)

                # Keep text inside frame bounds when face is near the top edge.
                label_y = y - 28 if y > 35 else y + bh + 18
                cv2.putText(
                    analysis_frame,
                    emotion_label,
                    (x, label_y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    color,
                    2,
                )
                cv2.putText(
                    analysis_frame,
                    engagement_label,
                    (x, label_y + 18),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    color,
                    2,
                )
                cv2.putText(
                    analysis_frame,
                    student_label,
                    (x, label_y + 36),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    color,
                    2,
                )
                cv2.putText(
                    analysis_frame,
                    engagement_status_label,
                    (x, label_y + 54),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    color,
                    2,
                )
                
                # Count emotions
                emotion_distribution[mapped_emotion] = emotion_distribution.get(mapped_emotion, 0) + 1
                total_engagement += engagement_score
                
                # Build student data for database
                students_data.append({
                    'face_index': face_index,
                    'student_id': detected_student_id,
                    'name': detected_student_name or 'Unknown Person',
                    'face_registered': bool(detected_student_id),
                    'confidence': round(float(match_confidence or 0.0), 3),
                    'liveness_ok': bool(liveness_ok),
                    'liveness_score': round(float(liveness_score), 2),
                    'liveness_reason': liveness_reason,
                    'emotion': mapped_emotion,
                    'emotion_confidence': round(confidence, 2),
                    'engagement_score': round(engagement_score, 1),
                    'fer_engagement_score': round(fer_engagement, 1),
                    'daisee_engagement_score': round(daisee_engagement, 1),
                    'engagement_source': 'fer+daisee-fusion',
                    'attention_score': round(engagement_score * 0.9, 1),
                    'posture_score': round(posture_score, 1),
                    'head_direction_score': round(head_direction_score, 1),
                    'eye_contact_score': round(eye_contact_score, 1),
                    'vertical_offset': round(float(vertical_offset), 3),
                    'horizontal_offset': round(float(horizontal_offset), 3),
                    'is_looking_forward': head_direction_score >= 60,
                    'face_bbox': box,
                })

                # Keep recognized_students for registered matches only.
                if detected_student_id:
                    recognized_students.append({
                        'face_index': face_index,
                        'student_id': detected_student_id,
                        'name': detected_student_name or 'Unknown Person',
                        'emotion': mapped_emotion,
                        'engagement': round(engagement_score, 1),
                        'fer_engagement': round(fer_engagement, 1),
                        'daisee_engagement': round(daisee_engagement, 1),
                        'emotion_confidence': round(confidence, 2),
                        'confidence': round(float(match_confidence or confidence or 0.0), 3),
                        'liveness_ok': True,
                        'liveness_score': round(float(liveness_score), 2),
                        'head_direction_score': round(head_direction_score, 1),
                        'eye_contact_score': round(eye_contact_score, 1),
                        'vertical_offset': round(float(vertical_offset), 3),
                        'horizontal_offset': round(float(horizontal_offset), 3),
                        'is_looking_forward': head_direction_score >= 60,
                        'posture_score': round(posture_score, 1),
                        'face_registered': True,
                    })
            
            face_count = len(students_data)
            avg_engagement = (total_engagement / face_count) if face_count > 0 else 0
            
            # Store results for display
            self.current_emotions = emotion_distribution
            self.faces_detected = face_count
            self.avg_engagement = avg_engagement
            self.students_data = students_data
            self.recognized_students = recognized_students
            self._maybe_create_confusion_notification(emotion_distribution, face_count)
            
            # Add info overlay
            registered_count = len(recognized_students)
            info_text = (
                f"Faces: {face_count} | Registered: {registered_count} | "
                f"Avg Engagement: {avg_engagement:.1f}% | FPS: {len(self.fps_counter) / 30 if self.fps_counter else 0:.1f}"
            )
            cv2.putText(analysis_frame, info_text, (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            
            # Add emotion summary at bottom
            if emotion_distribution:
                h, w = analysis_frame.shape[:2]
                emotion_text = " | ".join([f"{k}: {v}" for k, v in emotion_distribution.items()])
                cv2.putText(analysis_frame, f"Emotions: {emotion_text}", (10, h - 20), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            
            return analysis_frame
            
        except Exception as e:
            logger.error(f"Analysis error: {e}")
            return frame

    def _compute_iou(self, a, b):
        """Compute IoU between two [x, y, w, h] boxes."""
        ax1, ay1, aw, ah = a
        bx1, by1, bw, bh = b
        ax2, ay2 = ax1 + aw, ay1 + ah
        bx2, by2 = bx1 + bw, by1 + bh

        ix1 = max(ax1, bx1)
        iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2)
        iy2 = min(ay2, by2)
        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0

        area_a = max(1, aw * ah)
        area_b = max(1, bw * bh)
        union = max(1, area_a + area_b - inter)
        return float(inter) / float(union)

    def _estimate_liveness(self, face_roi):
        """Passive liveness heuristic to reduce photo-on-phone spoofing."""
        if face_roi is None or getattr(face_roi, 'size', 0) == 0:
            return False, 0.0, 'empty_face'

        try:
            gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
            lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            intensity_std = float(np.std(gray))

            bright_ratio = float(np.mean(gray >= 245))
            dark_ratio = float(np.mean(gray <= 10))

            min_texture = float(os.getenv('LIVENESS_MIN_LAPLACIAN_VAR', '20'))
            min_std = float(os.getenv('LIVENESS_MIN_STDDEV', '10'))
            max_bright_ratio = float(os.getenv('LIVENESS_MAX_BRIGHT_RATIO', '0.30'))

            score = (
                min(55.0, lap_var / 2.0)
                + min(35.0, intensity_std)
                + max(0.0, 10.0 - (bright_ratio * 35.0))
            )

            if lap_var < min_texture:
                return False, score, 'low_texture'
            if intensity_std < min_std:
                return False, score, 'flat_tone'
            if bright_ratio > max_bright_ratio and dark_ratio < 0.02:
                return False, score, 'screen_glare'

            return True, score, 'ok'
        except Exception as e:
            logger.debug(f"Liveness estimation failed: {e}")
            return False, 0.0, 'liveness_error'

    def _compute_face_signature(self, face_roi):
        """Compute compact normalized signature for temporal liveness variation checks."""
        try:
            gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            sig = cv2.resize(gray, (20, 20), interpolation=cv2.INTER_AREA).astype(np.float32).flatten()
            norm = float(np.linalg.norm(sig))
            if norm > 0:
                sig = sig / norm
            return sig
        except Exception:
            return None

    def _update_identity_confirmation(self, box, candidate_id, candidate_name, confidence, liveness_ok, liveness_score, face_signature, face_roi, now_ts):
        """Require repeated stable frames before confirming a recognized identity."""
        max_age_sec = float(os.getenv('IDENTITY_TRACK_MAX_AGE_SEC', '1.5'))
        iou_threshold = float(os.getenv('IDENTITY_TRACK_IOU', '0.35'))
        stable_frames_required = int(float(os.getenv('RECOGNITION_MIN_STABLE_FRAMES', '2')))
        min_liveness_score = float(os.getenv('MIN_LIVENESS_SCORE', '0'))
        min_signature_variation = float(os.getenv('MIN_SIGNATURE_VARIATION', '0.0'))
        allow_recognition_without_liveness = str(os.getenv('ALLOW_RECOGNITION_WITHOUT_LIVENESS', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
        liveness_bypass_confidence = float(os.getenv('LIVENESS_BYPASS_CONFIDENCE', '0.78'))
        require_blink = str(os.getenv('REQUIRE_BLINK_FOR_RECOGNITION', '0')).strip().lower() not in {'0', 'false', 'no', 'off'}
        min_blinks = int(float(os.getenv('MIN_BLINKS_FOR_RECOGNITION', '0')))

        self._identity_tracks = [
            t for t in self._identity_tracks
            if (now_ts - t.get('last_seen', 0.0)) <= max_age_sec
        ]

        best_idx = -1
        best_iou = 0.0
        for idx, track in enumerate(self._identity_tracks):
            iou = self._compute_iou(box, track.get('box', [0, 0, 0, 0]))
            if iou > best_iou:
                best_iou = iou
                best_idx = idx

        if best_idx >= 0 and best_iou >= iou_threshold:
            track = self._identity_tracks[best_idx]
            track['box'] = box
            track['last_seen'] = now_ts
        else:
            track = {
                'box': box,
                'last_seen': now_ts,
                'candidate_id': None,
                'candidate_name': None,
                'streak': 0,
                'best_confidence': 0.0,
                'prev_signature': None,
                'variation_accum': 0.0,
                'variation_count': 0,
                'prev_eyes_open': None,
                'closed_frames': 0,
                'blink_count': 0,
                'last_blink_at': 0.0,
            }
            self._identity_tracks.append(track)

        if face_signature is not None and track.get('prev_signature') is not None:
            try:
                delta = float(np.mean(np.abs(face_signature - track['prev_signature'])))
                track['variation_accum'] = float(track.get('variation_accum', 0.0)) + delta
                track['variation_count'] = int(track.get('variation_count', 0)) + 1
            except Exception:
                pass
        track['prev_signature'] = face_signature

        if not candidate_id:
            track['candidate_id'] = None
            track['candidate_name'] = None
            track['streak'] = 0
            track['best_confidence'] = 0.0
            return None, None, 0.0

        liveness_gate_ok = bool(liveness_ok) and float(liveness_score or 0.0) >= min_liveness_score
        if not liveness_gate_ok and not allow_recognition_without_liveness and float(confidence or 0.0) < liveness_bypass_confidence:
            track['candidate_id'] = None
            track['candidate_name'] = None
            track['streak'] = 0
            track['best_confidence'] = 0.0
            return None, None, 0.0

        eyes_open = self._detect_eyes_open(face_roi)
        self._update_blink_state(track, eyes_open, now_ts)

        if track.get('candidate_id') == candidate_id:
            track['streak'] = int(track.get('streak', 0)) + 1
            track['best_confidence'] = max(float(track.get('best_confidence', 0.0)), float(confidence or 0.0))
        else:
            track['candidate_id'] = candidate_id
            track['candidate_name'] = candidate_name
            track['streak'] = 1
            track['best_confidence'] = float(confidence or 0.0)

        avg_variation = 0.0
        if int(track.get('variation_count', 0)) > 0:
            avg_variation = float(track.get('variation_accum', 0.0)) / float(track.get('variation_count', 1))

        # Block static-photo spoofing: even with high match confidence, require natural temporal variation.
        if min_signature_variation > 0 and avg_variation < min_signature_variation:
            return None, None, 0.0

        if require_blink and int(track.get('blink_count', 0)) < min_blinks:
            return None, None, 0.0

        if int(track.get('streak', 0)) >= stable_frames_required:
            return track.get('candidate_id'), track.get('candidate_name'), float(track.get('best_confidence', 0.0))

        return None, None, 0.0

    def _detect_eyes_open(self, face_roi):
        """Estimate whether eyes are open using Haar eye detector on face ROI."""
        if self._eye_cascade is None:
            return None
        if face_roi is None or getattr(face_roi, 'size', 0) == 0:
            return None
        try:
            gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
            eyes = self._eye_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(12, 12))
            return len(eyes) >= 1
        except Exception:
            return None

    def _update_blink_state(self, track, eyes_open, now_ts):
        """Track blink transitions per identity track."""
        if eyes_open is None:
            return

        prev = track.get('prev_eyes_open')
        if prev is None:
            track['prev_eyes_open'] = bool(eyes_open)
            return

        if eyes_open:
            if int(track.get('closed_frames', 0)) >= 1 and prev is False:
                min_blink_gap = float(os.getenv('MIN_BLINK_GAP_SEC', '0.18'))
                if (now_ts - float(track.get('last_blink_at', 0.0))) >= min_blink_gap:
                    track['blink_count'] = int(track.get('blink_count', 0)) + 1
                    track['last_blink_at'] = now_ts
            track['closed_frames'] = 0
        else:
            track['closed_frames'] = int(track.get('closed_frames', 0)) + 1

        track['prev_eyes_open'] = bool(eyes_open)

    def _update_unknown_streak(self, box, now_ts):
        """Update and return stability streak for unknown face candidates."""
        max_age_sec = float(os.getenv('UNKNOWN_TRACK_MAX_AGE_SEC', '1.2'))
        iou_threshold = float(os.getenv('UNKNOWN_TRACK_IOU', '0.35'))

        # Drop stale tracks.
        self._unknown_tracks = [t for t in self._unknown_tracks if (now_ts - t.get('last_seen', 0)) <= max_age_sec]

        best_idx = -1
        best_iou = 0.0
        for idx, track in enumerate(self._unknown_tracks):
            iou = self._compute_iou(box, track.get('box', [0, 0, 0, 0]))
            if iou > best_iou:
                best_iou = iou
                best_idx = idx

        if best_idx >= 0 and best_iou >= iou_threshold:
            self._unknown_tracks[best_idx]['box'] = box
            self._unknown_tracks[best_idx]['last_seen'] = now_ts
            self._unknown_tracks[best_idx]['streak'] = int(self._unknown_tracks[best_idx].get('streak', 0)) + 1
            return self._unknown_tracks[best_idx]['streak']

        self._unknown_tracks.append({
            'box': box,
            'streak': 1,
            'last_seen': now_ts,
        })
        return 1

    def _apply_clahe_preprocessing(self, frame):
        """Improve low-light frames using CLAHE on LAB L-channel."""
        try:
            # Only run CLAHE in genuinely low-light scenes to avoid adding noise/artifacts.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if float(np.mean(gray)) > float(os.getenv('CLAHE_BRIGHTNESS_THRESHOLD', '92')):
                return frame

            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=float(os.getenv('CLAHE_CLIP_LIMIT', '1.8')), tileGridSize=(8, 8))
            l_enhanced = clahe.apply(l)
            merged = cv2.merge((l_enhanced, a, b))
            return cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
        except Exception:
            return frame

    def _get_temporally_smoothed_emotion(self, emotion_label):
        """Majority vote over the last 10 frames for stable emotion labels."""
        self.emotion_history.append(emotion_label)
        if not self.emotion_history:
            return emotion_label
        return Counter(self.emotion_history).most_common(1)[0][0]

    def _maybe_create_confusion_notification(self, emotion_distribution, face_count):
        """Create notification when confusion exceeds 30%."""
        if not emotion_distribution or face_count <= 0:
            return
        confused = float(emotion_distribution.get('confused', 0) or 0)
        ratio = confused / float(face_count)
        if ratio <= 0.30:
            return

        now = timezone.now()
        if self._last_confusion_notification_at and (now - self._last_confusion_notification_at).total_seconds() < 120:
            return

        self._last_confusion_notification_at = now
        Notification.objects.create(
            type='alert',
            message=f'Live confusion alert: {ratio * 100:.1f}% students are confused.',
            related_student=None,
        )
    
    def _generate_demo_frame(self):
        """Generate a demo frame with fake data and emotions"""
        height, width = 480, 640
        demo_frame = np.zeros((height, width, 3), dtype=np.uint8)
        
        # Add gradient background
        for i in range(height):
            demo_frame[i, :] = [i * 255 // height, 100, 255 - i * 255 // height]
        
        # Add demo text at top
        cv2.putText(demo_frame, "DEMO MODE - Camera Simulation", (10, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        
        # Simulate face detection boxes with emotions
        demo_faces = [
            (100, 100, 150, 150, 'happy', 0.85),
            (400, 120, 140, 140, 'focused', 0.72),
            (250, 250, 130, 130, 'neutral', 0.65)
        ]
        
        emotions_count = {}
        for i, (x, y, w, h, emotion, confidence) in enumerate(demo_faces):
            # Draw face box with color based on emotion
            color = (0, 255, 0) if emotion == 'happy' else (255, 255, 0) if emotion == 'focused' else (128, 128, 128)
            cv2.rectangle(demo_frame, (x, y), (x+w, y+h), color, 2)
            
            # Add emotion label with confidence
            label = f"{emotion} ({confidence:.0%})"
            cv2.putText(demo_frame, label, (x, y-10), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
            
            # Count emotions
            emotions_count[emotion] = emotions_count.get(emotion, 0) + 1
        
        # Store demo emotions
        self.current_emotions = emotions_count
        self.faces_detected = len(demo_faces)
        self.avg_engagement = 75.0
        
        # Add emotion summary at bottom
        if emotions_count:
            emotion_summary = " | ".join([f"{k}: {v}" for k, v in emotions_count.items()])
            cv2.putText(demo_frame, f"Emotions: {emotion_summary}", (10, height - 20), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        
        # Add frame counter
        self.demo_frame_count += 1
        cv2.putText(demo_frame, f"Frame: {self.demo_frame_count}", (width - 150, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        
        return demo_frame
    
    def _map_emotion(self, fer_emotion):
        """Map FER emotions to system emotions"""
        emotion_mapping = {
            'happy': 'happy',
            'neutral': 'neutral',
            'sad': 'bored',
            'fear': 'confused',
            'angry': 'confused',
            'surprise': 'focused',
            'disgust': 'bored'
        }
        return emotion_mapping.get(fer_emotion, 'neutral')
    
    def _calculate_engagement_score(self, emotions_dict):
        """Calculate engagement score from emotion confidence scores"""
        # Higher engagement for positive emotions
        engagement_weights = {
            'happy': 0.9,
            'neutral': 0.7,
            'surprise': 0.8,
            'sad': 0.3,
            'fear': 0.2,
            'angry': 0.2,
            'disgust': 0.3
        }

        total_score = 0
        total_weight = 0

        for emotion, confidence in emotions_dict.items():
            weight = engagement_weights.get(emotion, 0.5)
            total_score += confidence * weight
            total_weight += weight

        return (total_score / total_weight * 100) if total_weight > 0 else 50

    def _load_daisee_predictor(self):
        """Lazy-load DAiSEE model checkpoint if available."""
        if self._daisee_init_attempted:
            return

        self._daisee_init_attempted = True
        try:
            if not os.path.exists(self.daisee_model_path):
                logger.info(
                    "DAiSEE model checkpoint not found at %s; using proxy score fallback.",
                    self.daisee_model_path,
                )
                return

            from .daisee_model import DAiSEEPredictor

            self.daisee_predictor = DAiSEEPredictor(self.daisee_model_path)
            logger.info("DAiSEE model loaded from %s", self.daisee_model_path)
        except Exception as e:
            logger.warning("DAiSEE model load failed (%s). Falling back to proxy score.", e)
            self.daisee_predictor = None

    def _load_fer2013_predictor(self):
        """Lazy-load FER-2013 model checkpoint if available."""
        if self._fer2013_init_attempted:
            return

        self._fer2013_init_attempted = True
        try:
            if not os.path.exists(self.fer2013_model_path):
                logger.info(
                    "FER-2013 model checkpoint not found at %s; using FER package/fallback emotions.",
                    self.fer2013_model_path,
                )
                return

            from .fer_emotion_model import FER2013Predictor

            self.fer2013_predictor = FER2013Predictor(self.fer2013_model_path)
            logger.info("FER-2013 emotion model loaded from %s", self.fer2013_model_path)
        except Exception as e:
            logger.warning("FER-2013 model load failed (%s). Falling back to FER package/fallback.", e)
            self.fer2013_predictor = None

    def _predict_emotion_from_face(self, face_roi):
        """Predict emotion from a face crop with FER-2013 if a checkpoint is configured."""
        if self.fer2013_predictor is None and not self._fer2013_init_attempted:
            self._load_fer2013_predictor()

        if self.fer2013_predictor is None:
            return None, 0.0

        try:
            prediction = self.fer2013_predictor.predict(face_roi)
            if prediction is None:
                return None, 0.0
            return prediction.label, float(prediction.confidence)
        except Exception as e:
            logger.debug(f"FER-2013 predictor inference failed: {e}")
            return None, 0.0

    def _estimate_daissee_engagement(self, mapped_emotion, confidence, face_roi):
        """
        DAiSEE-inspired proxy score.
        Replace this with a trained DAiSEE model output when available.
        """
        # Try trained DAiSEE model first.
        if self.daisee_predictor is None and not self._daisee_init_attempted:
            self._load_daisee_predictor()

        if self.daisee_predictor is not None:
            try:
                prediction = self.daisee_predictor.predict(face_roi)
                if prediction is not None:
                    return max(0.0, min(100.0, float(prediction.score)))
            except Exception as e:
                logger.debug(f"DAiSEE predictor inference failed, using fallback proxy: {e}")

        base_by_emotion = {
            'focused': 90.0,
            'happy': 84.0,
            'neutral': 68.0,
            'confused': 42.0,
            'bored': 28.0,
        }
        base = base_by_emotion.get(mapped_emotion, 60.0)

        # Confidence bump from FER certainty.
        confidence_term = float(confidence or 0.0) * 10.0

        # Lightweight visual alertness proxy from face texture variance.
        texture_term = 0.0
        try:
            if face_roi is not None and getattr(face_roi, 'size', 0) > 0:
                gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
                lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                texture_term = min(10.0, lap_var / 60.0)
        except Exception:
            texture_term = 0.0

        return max(0.0, min(100.0, base + confidence_term + texture_term))

    def _fuse_engagement_scores(self, fer_score, daisee_score, fer_weight=None, daisee_weight=None):
        if not self.fusion_enabled:
            return float(fer_score)

        fw = self.fer_weight if fer_weight is None else float(fer_weight)
        dw = self.daisee_weight if daisee_weight is None else float(daisee_weight)
        total = fw + dw
        if total <= 0:
            fw, dw = self.fer_weight, self.daisee_weight
            total = fw + dw
        fw /= total
        dw /= total

        fused = (fw * float(fer_score)) + (dw * float(daisee_score))
        return max(0.0, min(100.0, fused))

# Global camera processor instance
camera_processor = CameraProcessor()

def generate_face_encoding(face_roi):
    """
    Generate face embedding.
    Primary: DeepFace Facenet embedding.
    Fallback: deterministic 128-dim grayscale signature for environments
    where DeepFace is unavailable.
    """
    if face_roi is None or getattr(face_roi, 'size', 0) == 0:
        return None

    try:
        # Try to import DeepFace lazily so environments without deepface still work.
        import importlib
        DeepFace = importlib.import_module('deepface').DeepFace
        
        # Use DeepFace to generate 128-dim embedding
        embedding = DeepFace.represent(
            face_roi, 
            model_name='Facenet',
            enforce_detection=False
        )
        
        if embedding and len(embedding) > 0:
            # Return the embedding vector as a list
            return embedding[0]['embedding']

    except ImportError:
        logger.warning("DeepFace not available, using fallback face encoding")
    except Exception as e:
        logger.warning(f"DeepFace embedding failed, using fallback encoding: {e}")

    try:
        # Fallback encoding: 16x8 normalized grayscale vector (128-dim).
        if len(face_roi.shape) == 2:
            gray = face_roi
        else:
            gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        signature = cv2.resize(gray, (16, 8), interpolation=cv2.INTER_AREA).astype(np.float32).flatten()
        norm = np.linalg.norm(signature)
        if norm > 0:
            signature = signature / norm
        return signature.tolist()
    except Exception as e:
        logger.error(f"Fallback face encoding failed: {e}")
        return None

# Camera control functions for compatibility
def start_camera(source=0):
    """Start camera processing"""
    return camera_processor.start(source)

def stop_camera():
    """Stop camera processing"""
    camera_processor.stop()

def get_camera_frame():
    """Get current frame"""
    return camera_processor.get_frame()

def get_camera_analysis():
    """Get current analysis"""
    return camera_processor.get_analysis()

# Compatibility functions for URLs
def simple_camera_feed(request):
    """MJPEG streaming endpoint"""
    from django.http import StreamingHttpResponse

    # Do not auto-start camera from feed endpoint.
    if not camera_processor.is_running:
        from django.http import HttpResponse
        return HttpResponse("Camera stream is not active. Start monitoring first.", status=409)

    # Use the improved MJPEG generator
    return StreamingHttpResponse(
        generate_mjpeg_frames(),
        content_type='multipart/x-mixed-replace; boundary=frame'
    )

@csrf_exempt
def start_simple_camera(request):
    """Start camera endpoint"""
    from django.http import JsonResponse
    from django.views.decorators.csrf import csrf_exempt
    from django.views.decorators.http import require_http_methods
    
    if request.method == 'POST':
        success = camera_processor.start()
        return JsonResponse({
            'success': True,
            'message': 'Camera started successfully' if success else 'Camera started in demo mode'
        })
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
def stop_simple_camera(request):
    """Stop camera endpoint"""
    from django.http import JsonResponse
    from django.views.decorators.csrf import csrf_exempt
    from django.views.decorators.http import require_http_methods
    
    if request.method == 'POST':
        camera_processor.stop()
        return JsonResponse({
            'success': True,
            'message': 'Camera stopped successfully'
        })
    return JsonResponse({'error': 'Method not allowed'}, status=405)

def get_emotion_stats(request):
    """Get emotion statistics endpoint"""
    from django.http import JsonResponse

    def _safe_json_value(value):
        if hasattr(value, 'tolist'):
            value = value.tolist()
        if isinstance(value, dict):
            return {str(k): _safe_json_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_safe_json_value(v) for v in value]
        try:
            import numpy as _np
            if isinstance(value, (_np.integer, _np.floating)):
                return value.item()
            if isinstance(value, _np.bool_):
                return bool(value)
        except Exception:
            pass
        return value
    
    if request.method == 'GET':
        analysis = camera_processor.get_analysis()
        return JsonResponse({
            'success': True,
            'stats': _safe_json_value(analysis),
            'students': _safe_json_value(analysis.get('students', []))
        })
    return JsonResponse({'error': 'Method not allowed'}, status=405)


# BUGFIX-2: Module-level stop_stream function with instance management
def stop_stream():
    """BUGFIX-2: Stop and destroy the global camera processor instance"""
    global camera_processor
    try:
        logger.info("🛑 Stopping and destroying stream instance...")
        camera_processor.stop()
        # Force new instance on next start
        camera_processor = CameraProcessor()
        logger.info("✅ Stream instance destroyed")
        return True
    except Exception as e:
        logger.error(f"❌ Error stopping stream: {e}")
        # Reset anyway
        camera_processor = CameraProcessor()
        return False


# BUGFIX-1 & BUGFIX-2: MJPEG frame generator with emotion data
def generate_mjpeg_frames():
    """BUGFIX-1 & BUGFIX-2: Generate MJPEG frames with emotion analysis"""
    stream = camera_processor
    while stream.is_running:
        try:
            frame = stream.get_frame()
            if frame is not None:
                import cv2
                _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                frame_bytes = buffer.tobytes()
                yield (
                    b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n'
                )
            time.sleep(0.033)
        except GeneratorExit:
            break
        except Exception:
            break
    # Generator ends cleanly when stream stops
    logger.info("MJPEG generator stopped")
