"""
PCU Hackthon - Enhanced Face Recognition System
Identifies registered students during live monitoring using advanced face matching
"""

import cv2
import numpy as np
import json
import logging
import os
from django.db import transaction
from django.utils import timezone
from engagement.models import Student, ClassSession, Attendance

logger = logging.getLogger(__name__)

class FaceRecognitionSystem:
    """
    Advanced face recognition system for identifying registered students
    """
    
    def __init__(self):
        self.student_encodings = {}
        self._deepface_checked = False
        self._deepface_enabled = str(os.getenv('ENABLE_DEEPFACE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
        self._deepface_backend = None
        self.load_student_encodings()
        logger.info("✅ FaceRecognitionSystem initialized")

    def _ensure_deepface(self):
        """Initialize DeepFace lazily only once to avoid repeated heavy startup cost."""
        if self._deepface_checked:
            return self._deepface_backend

        self._deepface_checked = True
        if not self._deepface_enabled:
            logger.info("DeepFace disabled by ENABLE_DEEPFACE env var; using fallback face encoding")
            return None

        try:
            from deepface import DeepFace 
            self._deepface_backend = DeepFace
            logger.info("DeepFace backend initialized for face encoding")
        except ImportError:
            logger.warning("DeepFace not installed; using fallback face encoding")
            self._deepface_backend = None
        except Exception as e:
            logger.warning(f"DeepFace init failed; using fallback face encoding: {e}")
            self._deepface_backend = None

        return self._deepface_backend
    
    def load_student_encodings(self):
        """Load all active student face encodings from database."""
        try:
            logger.info("Loading registered students from database...")
            self.student_encodings.clear()

            students = Student.objects.filter(is_active=True, face_encoding__isnull=False).exclude(face_encoding='')

            for student in students:
                try:
                    face_encoding_data = json.loads(student.face_encoding)

                    if isinstance(face_encoding_data, list) and face_encoding_data and isinstance(face_encoding_data[0], list):
                        encodings = [np.array(item, dtype=np.float32) for item in face_encoding_data if item]
                    elif isinstance(face_encoding_data, list):
                        encodings = [np.array(face_encoding_data, dtype=np.float32)]
                    else:
                        logger.warning(f"Invalid encoding format for {student.name}")
                        continue

                    encodings = [enc for enc in encodings if enc.size > 0]
                    if not encodings:
                        continue

                    self.student_encodings[student.student_id] = {
                        'student_id': student.student_id,
                        'name': student.name,
                        'encodings': encodings,
                        'email': student.email,
                        'seat_row': student.seat_row,
                        'seat_col': student.seat_col,
                    }

                    logger.info(f"Loaded student: {student.name} ({student.student_id}) with {len(encodings)} encoding(s)")
                except Exception as e:
                    logger.error(f"Error loading face encoding for {student.name}: {e}")
                    continue

            logger.info(f"Total students loaded: {len(self.student_encodings)}")
            return True
        except Exception as e:
            logger.error(f"Error loading known faces: {e}")
            return False
    
    def identify_student(self, face_roi, confidence_threshold=0.62):
        """
        Identify a student from face ROI with improved accuracy
        Returns: (student_id, student_name, confidence) or (None, None, 0)
        """
        try:
            if not self.student_encodings:
                logger.warning("❌ No student encodings loaded")
                return None, None, 0
            
            # Generate encoding for detected face
            face_encoding = self.generate_face_encoding(face_roi)
            if face_encoding is None:
                logger.warning("❌ Failed to generate face encoding for detected face")
                return None, None, 0
            
            face_encoding = np.array(face_encoding)
            logger.info(f"Analyzing detected face with encoding length: {len(face_encoding)}")

            best_match = None
            best_match_student_id = None
            best_confidence = 0
            second_best_confidence = 0
            all_similarities = []

            for student_id, student_data in self.student_encodings.items():
                try:
                    stored_encodings = student_data.get('encodings', [])
                    best_student_confidence = 0.0

                    for stored_encoding in stored_encodings:
                        stored_encoding = np.array(stored_encoding, dtype=np.float32)
                        if stored_encoding.shape != face_encoding.shape:
                            logger.debug(
                                "Skipping %s due to encoding shape mismatch (detected=%s, stored=%s)",
                                student_id,
                                face_encoding.shape,
                                stored_encoding.shape,
                            )
                            continue

                        similarity = self.calculate_similarity(face_encoding, stored_encoding)
                        if similarity > best_student_confidence:
                            best_student_confidence = similarity

                    if best_student_confidence <= 0:
                        continue

                    all_similarities.append((student_id, student_data['name'], best_student_confidence))

                    if best_student_confidence > best_confidence:
                        second_best_confidence = best_confidence
                        best_confidence = best_student_confidence
                        best_match = student_data
                        best_match_student_id = student_id
                    elif best_student_confidence > second_best_confidence:
                        second_best_confidence = best_student_confidence
                except Exception as e:
                    logger.error(f"Error comparing with student {student_id}: {e}")
                    continue

            all_similarities.sort(key=lambda x: x[2], reverse=True)
            logger.info(f"Top 3 matches: {all_similarities[:3]}")

            confidence_margin = best_confidence - second_best_confidence
            has_single_candidate = len(all_similarities) <= 1
            strict_margin_ok = confidence_margin >= 0.03
            high_conf_relaxed_margin_ok = (best_confidence >= 0.85 and confidence_margin >= 0.015)
            very_high_confidence_ok = best_confidence >= 0.94

            if best_match and best_confidence >= confidence_threshold and (
                has_single_candidate or strict_margin_ok or high_conf_relaxed_margin_ok or very_high_confidence_ok
            ):
                return best_match_student_id, best_match['name'], float(best_confidence)

            logger.info(
                "No reliable match: best=%.3f second=%.3f margin=%.3f threshold=%.2f",
                best_confidence,
                second_best_confidence,
                confidence_margin,
                confidence_threshold,
            )
            return None, None, 0
            
        except Exception as e:
            logger.error(f"❌ Face identification error: {e}")
            return None, None, 0
    
    def generate_face_encoding(self, face_roi):
        """Generate face embedding with DeepFace and a deterministic fallback."""
        if face_roi is None or getattr(face_roi, 'size', 0) == 0:
            return None

        deepface_backend = self._ensure_deepface()
        if deepface_backend is not None:
            try:
                # Generate embedding using DeepFace when available.
                embedding = deepface_backend.represent(
                    face_roi,
                    model_name='Facenet',
                    enforce_detection=False,
                )

                if embedding and len(embedding) > 0 and 'embedding' in embedding[0]:
                    return embedding[0]['embedding']
            except Exception as e:
                # Disable DeepFace after a runtime failure to keep live stream responsive.
                logger.warning(f"DeepFace embedding failed; switching to fallback encoding: {e}")
                self._deepface_backend = None

        try:
            # Fallback encoding: same 128-dim signature used during registration.
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
            logger.error(f"❌ Fallback face encoding error: {e}")
            return None
    
    def calculate_similarity(self, encoding1, encoding2):
        """Calculate similarity between two face encodings using cosine similarity"""
        try:
            # FIXED: Updated for DeepFace embeddings (128-dim vectors)
            enc1 = np.array(encoding1)
            enc2 = np.array(encoding2)

            # Use cosine similarity for DeepFace embeddings
            dot_product = np.dot(enc1, enc2)
            norm1 = np.linalg.norm(enc1)
            norm2 = np.linalg.norm(enc2)
            
            if norm1 == 0 or norm2 == 0:
                return 0
            
            similarity = dot_product / (norm1 * norm2)
            
            # Ensure similarity is between 0 and 1
            return max(0, min(1, similarity))
            
        except Exception as e:
            logger.error(f"Similarity calculation error: {e}")
            return 0
    
    def refresh_encodings(self):
        """Refresh student encodings from database"""
        self.student_encodings.clear()
        self.load_student_encodings()
        logger.info("✅ Student encodings refreshed")

# Global instance
_face_recognition_system = None

def get_face_recognition_system():
    """Get global face recognition system instance"""
    global _face_recognition_system
    if _face_recognition_system is None:
        _face_recognition_system = FaceRecognitionSystem()
    return _face_recognition_system
