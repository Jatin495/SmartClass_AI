"""
PCU Hackthon - REST API Views
All endpoints that the frontend HTML/JS communicates with.
"""

import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timedelta

from django.http import StreamingHttpResponse, JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Avg, Count, Max, Q
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password, check_password
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
import pandas as pd

from .models import (
    Teacher, Student, ClassSession, Attendance,
    EngagementRecord, ClassEngagementSnapshot, Alert, Report,
    SyllabusTopic, DailyLectureTopic, StudentTopicProgress,
    ExtraLecturePlan, LectureFeedback, Notification, AIInsight,
    Syllabus, LecturePlan, Checkpoint, CheckpointResult,
    ExtraLecture, Feedback, TeacherProfile, ActivityLog, Timetable,
)
from .camera import generate_face_encoding

logger = logging.getLogger(__name__)
User = get_user_model()

_ENGAGEMENT_WRITE_INTERVAL_SECONDS = 5
_last_engagement_write_by_student = {}
_last_snapshot_write_by_session = {}
_engagement_write_lock = threading.Lock()
_active_session_topic_map = {}
_student_behavior_state = {}
_last_behavior_alert_at = {}
_BEHAVIOR_ALERT_COOLDOWN_SECONDS = 20
_AUTO_RESTART_SUPPRESS_UNTIL = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def hash_password(password):
    return make_password(password)


def verify_and_upgrade_password(teacher, raw_password):
    """
    Verify password using Django hashers and transparently upgrade legacy
    unsalted SHA-256 hashes to Django's default hasher on successful login.
    """
    stored = str(teacher.password_hash or '')
    if not raw_password:
        return False

    # First try Django-standard password hash verification.
    if check_password(raw_password, stored):
        return True

    # Legacy compatibility path: previous versions stored SHA-256 hex digests.
    # Keep this strictly as a migration bridge and upgrade on success.
    import hashlib
    legacy_sha256 = hashlib.sha256(raw_password.encode()).hexdigest()
    if stored == legacy_sha256:
        teacher.password_hash = make_password(raw_password)
        teacher.save(update_fields=['password_hash'])
        return True

    return False

def json_response(data, status_code=200):
    return JsonResponse(data, status=status_code, safe=False)


def _create_notification_if_needed(notification_type, message, related_student=None, dedupe_hours=6):
    """Create notification with basic dedupe window to avoid flooding."""
    since = timezone.now() - timedelta(hours=dedupe_hours)
    existing = Notification.objects.filter(
        type=notification_type,
        message=message,
        created_at__gte=since,
        related_student=related_student,
    ).exists()
    if not existing:
        Notification.objects.create(
            type=notification_type,
            message=message,
            related_student=related_student,
        )


def _mark_attendance_from_face_detections(active_session, detected_students):
    """Mark attendance as present for recognized students seen in the live feed."""
    if not active_session or not detected_students:
        return 0

    today = timezone.now().date()
    now = timezone.now()

    # Keep strongest confidence per recognized student_id in this batch.
    confidence_by_student_id = {}
    for item in detected_students:
        sid = item.get('student_id')
        if not sid:
            continue
        confidence = float(item.get('confidence') or 0)
        previous = confidence_by_student_id.get(sid, 0)
        if confidence > previous:
            confidence_by_student_id[sid] = confidence

    if not confidence_by_student_id:
        return 0

    students = Student.objects.filter(
        is_active=True,
        student_id__in=list(confidence_by_student_id.keys())
    )

    updated_count = 0
    for student in students:
        conf = confidence_by_student_id.get(student.student_id, 0.0)
        attendance, created = Attendance.objects.get_or_create(
            student=student,
            session=active_session,
            date=today,
            defaults={
                'is_present': True,
                'arrival_time': now,
                'detection_confidence': conf,
            }
        )

        if created:
            updated_count += 1
            continue

        changed = False
        if not attendance.is_present:
            attendance.is_present = True
            changed = True
        if attendance.arrival_time is None:
            attendance.arrival_time = now
            changed = True
        if conf > float(attendance.detection_confidence or 0):
            attendance.detection_confidence = conf
            changed = True

        if changed:
            attendance.save(update_fields=['is_present', 'arrival_time', 'detection_confidence'])
            updated_count += 1

    return updated_count


def _normalize_emotion(emotion):
    """Normalize detector emotion labels to EngagementRecord choices."""
    normalized = str(emotion or 'unknown').strip().lower()
    allowed = {
        'happy', 'neutral', 'sad', 'angry', 'surprise',
        'fear', 'disgust', 'confused', 'bored', 'focused', 'unknown'
    }
    return normalized if normalized in allowed else 'unknown'


def _first_value(*values):
    """Return first meaningful value, handling numpy-like arrays safely."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value == '':
            continue

        # Handle numpy arrays and array-like objects without importing numpy.
        if hasattr(value, 'shape') and hasattr(value, 'flatten'):
            try:
                size = getattr(value, 'size', None)
                if size == 0:
                    continue
                flat = value.flatten()
                if len(flat) == 0:
                    continue
                return flat[0]
            except Exception:
                continue

        return value

    return None


def _safe_float(*values, default=0.0):
    value = _first_value(*values)
    if value is None:
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_bool(*values, default=False):
    value = _first_value(*values)
    if value is None:
        return bool(default)
    try:
        return bool(value)
    except Exception:
        return bool(default)


def _persist_live_engagement_records(active_session, analysis, now):
    """
    Persist per-student engagement records and periodic class snapshots from live analysis.
    Throttles writes to reduce DB pressure while keeping timeline and reports current.
    """
    if not active_session or not analysis:
        return {'records_written': 0, 'snapshot_written': False}

    students_payload = analysis.get('students') or analysis.get('recognized_students') or []
    if not students_payload:
        return {'records_written': 0, 'snapshot_written': False}

    now_ts = now.timestamp()
    valid_student_ids = [s.get('student_id') for s in students_payload if s.get('student_id')]
    students_by_sid = {
        s.student_id: s for s in Student.objects.filter(is_active=True, student_id__in=valid_student_ids)
    }

    records_written = 0
    with _engagement_write_lock:
        for item in students_payload:
            sid = item.get('student_id')
            if not sid:
                continue

            student = students_by_sid.get(sid)
            if not student:
                continue

            student_key = f"{active_session.id}:{sid}"
            last_student_write = _last_engagement_write_by_student.get(student_key, 0)
            if (now_ts - last_student_write) < _ENGAGEMENT_WRITE_INTERVAL_SECONDS:
                continue

            emotion = _normalize_emotion(item.get('emotion'))
            emotion_conf = _safe_float(item.get('emotion_confidence'), item.get('confidence'), default=0.0)
            engagement_score = _safe_float(item.get('engagement_score'), item.get('engagement'), default=0.0)
            attention_score = _safe_float(item.get('attention_score'), default=(engagement_score * 0.9))
            posture_score = _safe_float(item.get('posture_score'), default=0.0)
            eye_contact = _safe_bool(item.get('is_looking_forward'), default=False)

            face_bbox_val = item.get('face_bbox')
            if hasattr(face_bbox_val, 'tolist'):
                try:
                    face_bbox_val = face_bbox_val.tolist()
                except Exception:
                    pass

            if isinstance(face_bbox_val, (list, tuple)):
                face_bbox = ','.join(str(int(float(v))) for v in face_bbox_val[:4])
            else:
                face_bbox = str(face_bbox_val) if face_bbox_val is not None else ''

            emotion_scores = item.get('emotion_scores')
            if not isinstance(emotion_scores, dict):
                emotion_scores = {emotion: round(emotion_conf, 3)}

            EngagementRecord.objects.create(
                student=student,
                session=active_session,
                timestamp=now,
                engagement_score=round(engagement_score, 1),
                attention_score=round(attention_score, 1),
                emotion=emotion,
                emotion_confidence=round(emotion_conf, 3),
                emotion_scores=json.dumps(emotion_scores),
                head_angle=float(item.get('head_angle') or 0.0),
                eye_contact=eye_contact,
                posture_score=round(posture_score, 1),
                is_slouching=_safe_bool(item.get('is_slouching'), default=(posture_score > 0 and posture_score < 50)),
                face_detected=True,
                face_confidence=round(_safe_float(item.get('confidence'), emotion_conf, default=0.0), 3),
                face_bbox=face_bbox,
                frame_path=str(item.get('frame_path') or ''),
            )

            _last_engagement_write_by_student[student_key] = now_ts
            records_written += 1

        snapshot_written = False
        snapshot_key = str(active_session.id)
        last_snapshot_write = _last_snapshot_write_by_session.get(snapshot_key, 0)
        if (now_ts - last_snapshot_write) >= _ENGAGEMENT_WRITE_INTERVAL_SECONDS:
            avg_eng = float(analysis.get('avg_engagement') or 0.0)
            avg_att = float(analysis.get('avg_attention') or (avg_eng * 0.9))
            present_count = int(analysis.get('present_count') or 0)
            emotion_distribution = analysis.get('emotion_distribution') or analysis.get('emotions') or {}
            if not isinstance(emotion_distribution, dict):
                emotion_distribution = {}

            confusion_ratio = 0.0
            if present_count > 0:
                confusion_count = float(emotion_distribution.get('confused', 0) or 0)
                confusion_ratio = confusion_count / present_count

            ClassEngagementSnapshot.objects.create(
                session=active_session,
                timestamp=now,
                avg_engagement=round(avg_eng, 1),
                avg_attention=round(avg_att, 1),
                present_count=present_count,
                emotion_distribution=json.dumps(emotion_distribution),
                confusion_alert=confusion_ratio >= 0.30,
                low_engagement_alert=avg_eng < 60,
            )
            _last_snapshot_write_by_session[snapshot_key] = now_ts
            snapshot_written = True

    return {'records_written': records_written, 'snapshot_written': snapshot_written}


# ─── Auth Endpoints ───────────────────────────────────────────────────────────

@csrf_exempt
@api_view(['POST'])
def login(request):
    """Teacher login endpoint"""
    try:
        data = request.data
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')

        if not email or not password:
            return Response({'error': 'Email and password required'}, status=400)

        teacher = Teacher.objects.filter(email=email, is_active=True).first()

        if not teacher:
            teacher = Teacher.objects.create(
                name='Demo Teacher',
                email=email,
                password_hash=hash_password(password),
                subject='Computer Science'
            )
            logger.info(f"Auto-created teacher: {email}")

        if not verify_and_upgrade_password(teacher, password):
            return Response({'error': 'Invalid credentials'}, status=401)

        request.session['teacher_id'] = teacher.id
        request.session['teacher_name'] = teacher.name
        request.session.modified = True

        return Response({
            'success': True,
            'teacher': {
                'id': teacher.id,
                'name': teacher.name,
                'email': teacher.email,
                'subject': teacher.subject,
            }
        })
    except Exception as e:
        logger.error(f"Login error: {e}")
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
def logout_view(request):
    request.session.flush()
    return Response({'success': True})


# ─── Dashboard Endpoints ──────────────────────────────────────────────────────

@api_view(['GET'])
def dashboard_stats(request):
    try:
        today = timezone.now().date()
        engagement_today = timezone.localdate()
        total_students = Student.objects.filter(is_active=True).count()
        present_today = Attendance.objects.filter(date=today, is_present=True).values('student').distinct().count()
        active_session = ClassSession.objects.filter(status='active').first()
        today_records = EngagementRecord.objects.filter(timestamp__date=engagement_today)
        avg_engagement = today_records.aggregate(avg=Avg('engagement_score'))['avg'] or 0
        active_alerts = Alert.objects.filter(is_resolved=False).count()

        thirty_min_ago = timezone.now() - timedelta(minutes=30)
        recent_records = EngagementRecord.objects.filter(timestamp__gte=thirty_min_ago)
        emotion_dist = {}
        for record in recent_records:
            e = record.emotion
            emotion_dist[e] = emotion_dist.get(e, 0) + 1

        return Response({
            'total_students': total_students,
            'present_today': present_today,
            'avg_engagement': round(avg_engagement, 1),
            'active_alerts': active_alerts,
            'active_session': None if not active_session else {
                'id': active_session.id,
                'name': active_session.class_name,
                'duration': active_session.duration_minutes
            },
            'emotion_distribution': emotion_dist,
            'date': today.isoformat(),
        })
    except Exception as e:
        logger.error(f"Dashboard stats error: {e}")
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def engagement_timeline(request):
    try:
        session_id = request.query_params.get('session_id')
        one_hour_ago = timezone.now() - timedelta(hours=1)
        snapshots = ClassEngagementSnapshot.objects.filter(timestamp__gte=one_hour_ago)
        if session_id:
            snapshots = snapshots.filter(session_id=session_id)
        snapshots = snapshots.order_by('timestamp')
        data = [{
            'time': timezone.localtime(s.timestamp).strftime('%H:%M:%S'),
            'engagement': round(s.avg_engagement, 1),
            'attention': round(s.avg_attention, 1),
            'present': s.present_count,
        } for s in snapshots]
        return Response({'timeline': data})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Students Endpoints ───────────────────────────────────────────────────────

@api_view(['GET'])
def list_students(request):
    try:
        from django.db.models import OuterRef, Subquery
        
        # FIXED: Use Subquery to avoid N+1 queries
        today = timezone.now().date()
        
        # Subquery for latest engagement record per student
        latest_engagement = EngagementRecord.objects.filter(
            student=OuterRef('pk')
        ).order_by('-timestamp')
        
        # A student is considered present today if present in any session today.
        present_student_ids = set(
            Attendance.objects.filter(date=today, is_present=True).values_list('student_id', flat=True)
        )
        
        # Single query with annotations
        students = Student.objects.filter(is_active=True).annotate(
            latest_emotion=Subquery(latest_engagement.values('emotion')[:1]),
            latest_engagement_score=Subquery(latest_engagement.values('engagement_score')[:1]),
            latest_timestamp=Subquery(latest_engagement.values('timestamp')[:1])
        )
        
        result = []
        for student in students:
            result.append({
                'id': student.id,
                'student_id': student.student_id,
                'name': student.name,
                'email': student.email,
                'seat_row': student.seat_row,
                'seat_col': student.seat_col,
                'face_registered': bool(student.face_encoding),
                'present_today': student.id in present_student_ids,
                'current_emotion': student.latest_emotion or 'unknown',
                'current_engagement': round(student.latest_engagement_score, 1) if student.latest_engagement_score else 0,
                'avg_engagement': round(student.latest_engagement_score, 1) if student.latest_engagement_score else 0,
                'last_updated': student.latest_timestamp.isoformat() if student.latest_timestamp else None,
            })
        
        return Response({'students': result, 'total': len(result)})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def students_overview(request):
    """
    Aggregated student page metrics for Chart.js.
    Returns attendance overview (last 4 weeks), engagement distribution (latest per student),
    and class performance trend (last 7 days).
    """
    try:
        now = timezone.now()
        today = now.date()

        # ── Attendance overview: last 28 days grouped into 4 weeks ────────────
        start_date = today - timedelta(days=27)
        attendance_qs = Attendance.objects.filter(date__gte=start_date, date__lte=today).only('date', 'is_present', 'student_id')
        # Aggregate by day using unique students (sessions can create duplicates).
        per_day = {}
        for a in attendance_qs:
            d = a.date
            if d not in per_day:
                per_day[d] = {'present': set(), 'absent': set()}
            if a.is_present:
                per_day[d]['present'].add(a.student_id)
            else:
                per_day[d]['absent'].add(a.student_id)

        # Then group days into 4 week buckets and average per day.
        present_sum = [0, 0, 0, 0]
        absent_sum = [0, 0, 0, 0]
        days_count = [0, 0, 0, 0]
        for d, sets in per_day.items():
            days_ago = (today - d).days
            bucket = 3 - min(3, max(0, days_ago // 7))
            present_sum[bucket] += len(sets['present'])
            absent_sum[bucket] += len(sets['absent'])
            days_count[bucket] += 1

        present_by_week = [
            round(present_sum[i] / days_count[i], 1) if days_count[i] else 0 for i in range(4)
        ]
        absent_by_week = [
            round(absent_sum[i] / days_count[i], 1) if days_count[i] else 0 for i in range(4)
        ]

        attendance_overview = {
            'labels': ['Week 1', 'Week 2', 'Week 3', 'Week 4'],
            'present': present_by_week,
            'absent': absent_by_week,
        }

        # ── Engagement distribution: latest record per student (fallback 0) ──
        students = Student.objects.filter(is_active=True).only('id')
        bins_labels = ['90-100%', '80-89%', '70-79%', '60-69%', 'Below 60%']
        bins_counts = [0, 0, 0, 0, 0]

        for s in students:
            latest = EngagementRecord.objects.filter(student=s).order_by('-timestamp').only('engagement_score').first()
            score = float(latest.engagement_score) if latest and latest.engagement_score is not None else 0.0
            if score >= 90:
                bins_counts[0] += 1
            elif score >= 80:
                bins_counts[1] += 1
            elif score >= 70:
                bins_counts[2] += 1
            elif score >= 60:
                bins_counts[3] += 1
            else:
                bins_counts[4] += 1

        engagement_distribution = {
            'labels': bins_labels,
            'counts': bins_counts,
        }

        # ── Class performance: last 7 days averages by weekday ───────────────
        since = now - timedelta(days=7)
        recs = EngagementRecord.objects.filter(timestamp__gte=since).values(
            'timestamp', 'engagement_score', 'attention_score'
        )

        weekday_labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        eng_sum = [0.0] * 7
        att_sum = [0.0] * 7
        eng_cnt = [0] * 7
        att_cnt = [0] * 7

        for r in recs:
            ts = r.get('timestamp')
            if not ts:
                continue
            idx = ts.weekday()  # Mon=0..Sun=6
            e = r.get('engagement_score')
            a = r.get('attention_score')
            if e is not None:
                eng_sum[idx] += float(e)
                eng_cnt[idx] += 1
            if a is not None:
                att_sum[idx] += float(a)
                att_cnt[idx] += 1

        engagement_avg = [round((eng_sum[i] / eng_cnt[i]), 1) if eng_cnt[i] else 0 for i in range(7)]
        attention_avg = [round((att_sum[i] / att_cnt[i]), 1) if att_cnt[i] else 0 for i in range(7)]

        class_performance = {
            'labels': weekday_labels,
            'avg_engagement': engagement_avg,
            'avg_attention': attention_avg,
        }

        return Response({
            'attendance_overview': attendance_overview,
            'engagement_distribution': engagement_distribution,
            'class_performance': class_performance,
            'generated_at': now.isoformat(),
        })
    except Exception as e:
        logger.error(f"Students overview error: {e}")
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def add_student(request):
    try:
        data = request.data
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        seat_row = data.get('seat_row', 1)
        seat_col = data.get('seat_col', 1)
        face_image = request.FILES.get('face_image')  # Face image for registration
        if not name:
            return Response({'error': 'Name is required'}, status=400)

        if not face_image:
            return Response({'error': 'Face image is required for student registration'}, status=400)

        count = Student.objects.count() + 1
        student_id = f"STU{count:03d}"

        # Process face encoding if image provided
        face_encoding = None
        if face_image:
            try:
                import cv2
                import numpy as np
                
                # Read and process face image
                image_bytes = face_image.read()
                nparr = np.frombuffer(image_bytes, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                if img is not None:
                    face_roi = None

                    # Strict registration checks: exactly one clear, frontal face.
                    try:
                        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                        h_img, w_img = img.shape[:2]
                        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
                        faces = cascade.detectMultiScale(
                            gray,
                            scaleFactor=1.1,
                            minNeighbors=5,
                            minSize=(60, 60),
                        )

                        if len(faces) == 0:
                            logger.warning(f"No frontal face detected for {name}; registration rejected")
                            return Response({'error': 'No clear frontal face detected. Look straight at camera and try again.'}, status=400)

                        if len(faces) > 1:
                            logger.warning(f"Multiple faces detected for {name}; registration rejected")
                            return Response({'error': 'Multiple faces detected. Keep only one person in frame.'}, status=400)

                        x, y, w, h = faces[0]
                        face_roi = img[y:y+h, x:x+w]

                        # Quality gates for robust live recognition.
                        face_gray = gray[y:y+h, x:x+w]
                        blur_score = float(cv2.Laplacian(face_gray, cv2.CV_64F).var()) if face_gray.size else 0.0
                        brightness = float(np.mean(face_gray)) if face_gray.size else 0.0
                        face_area_ratio = float((w * h) / float(max(1, w_img * h_img)))

                        if face_area_ratio < 0.09:
                            return Response({'error': 'Face is too small. Move closer and recapture.'}, status=400)
                        if blur_score < 70.0:
                            return Response({'error': 'Image is blurry. Hold still and recapture.'}, status=400)
                        if brightness < 45.0 or brightness > 220.0:
                            return Response({'error': 'Lighting is not suitable. Ensure face is clearly lit.'}, status=400)

                    except Exception as detect_error:
                        logger.warning(f"OpenCV face detection failed for {name}: {detect_error}")

                    # Do not silently fallback to center/full image because it can
                    # register a non-face patch and later produce wrong identity matches.
                    if face_roi is None or getattr(face_roi, 'size', 0) == 0:
                        logger.warning(f"No valid face detected for {name}; registration rejected")
                        return Response({'error': 'No clear face detected. Please upload a front-facing image with one visible face.'}, status=400)

                    if face_roi is not None and getattr(face_roi, 'size', 0) > 0:
                        face_encoding = generate_face_encoding(face_roi)

                    if face_encoding:
                        logger.info(f"✅ Face registered for student {name}")
                    else:
                        logger.error(f"Failed to generate encoding for {name}")
                else:
                    logger.error(f"Could not decode face image for {name}")
                    
            except Exception as e:
                logger.error(f"Face processing error for {name}: {e}")
        
        if face_encoding is None:
            return Response({'error': 'Face registration failed. Please upload a clear student face image.'}, status=400)

        student = Student.objects.create(
            student_id=student_id, name=name, email=email,
            seat_row=seat_row, seat_col=seat_col,
            face_encoding=json.dumps(face_encoding) if face_encoding is not None else None
        )

        # Keep the live recognition cache in sync with newly registered students.
        if face_encoding is not None:
            try:
                from .face_recognition import get_face_recognition_system
                get_face_recognition_system().refresh_encodings()
            except Exception as refresh_error:
                logger.warning(f"Face cache refresh failed for {name}: {refresh_error}")
        
        return Response({'success': True, 'student': {
            'id': student.id, 'student_id': student.student_id, 'name': student.name,
            'face_registered': face_encoding is not None
        }})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def student_detail(request, student_id):
    try:
        student = Student.objects.get(student_id=student_id, is_active=True)
        seven_days = timezone.now() - timedelta(days=7)
        records = EngagementRecord.objects.filter(student=student, timestamp__gte=seven_days).order_by('timestamp')

        if records.exists():
            df = pd.DataFrame(list(records.values('timestamp', 'engagement_score', 'attention_score', 'emotion')))
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df['date'] = df['timestamp'].dt.date
            daily_avg = df.groupby('date').agg({'engagement_score': 'mean', 'attention_score': 'mean'}).reset_index()
            engagement_history = [{
                'date': str(row['date']),
                'avg_engagement': round(row['engagement_score'], 1),
                'avg_attention': round(row['attention_score'], 1),
            } for _, row in daily_avg.iterrows()]
            emotion_freq = df['emotion'].value_counts().to_dict()
        else:
            engagement_history = []
            emotion_freq = {}

        thirty_days = timezone.now().date() - timedelta(days=30)
        attendance = Attendance.objects.filter(student=student, date__gte=thirty_days)
        attend_rate = attendance.filter(is_present=True).count() / max(attendance.count(), 1) * 100

        return Response({
            'student': {
                'id': student.id, 'student_id': student.student_id,
                'name': student.name, 'email': student.email,
                'seat_row': student.seat_row, 'seat_col': student.seat_col,
            },
            'engagement_history': engagement_history,
            'emotion_frequency': emotion_freq,
            'attendance_rate': round(attend_rate, 1),
        })
    except Student.DoesNotExist:
        return Response({'error': 'Student not found'}, status=404)
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
def face_capture_check(request):
    """
    Validate guided registration frame quality and pose-position step.
    expected_pose is forced to straight (left/right disabled).
    """
    try:
        expected_pose = 'straight'

        face_image = request.FILES.get('face_image')
        if not face_image:
            return Response({'success': False, 'error': 'Face image is required'}, status=400)

        import cv2
        import numpy as np

        image_bytes = face_image.read()
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            return Response({'success': False, 'error': 'Could not decode image'}, status=400)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h_img, w_img = img.shape[:2]

        candidates = []

        # 1) Frontal Haar detection
        try:
            face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
            frontal_faces = face_cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=3,
                minSize=(50, 50),
            )
            for (fx, fy, fw, fh) in frontal_faces:
                candidates.append({
                    'box': (int(fx), int(fy), int(fw), int(fh)),
                    'source': 'haar_frontal',
                    'pose_hint': 'straight',
                })
        except Exception:
            pass

        # 2) Profile Haar detection (original + flipped)
        try:
            profile_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_profileface.xml')

            # Detector on original frame
            profile_faces = profile_cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=3,
                minSize=(50, 50),
            )
            for (px, py, pw, ph) in profile_faces:
                candidates.append({
                    'box': (int(px), int(py), int(pw), int(ph)),
                    'source': 'haar_profile_original',
                    'pose_hint': 'right',
                })

            # Detector on mirrored frame (map back to original x)
            gray_flip = cv2.flip(gray, 1)
            profile_faces_flip = profile_cascade.detectMultiScale(
                gray_flip,
                scaleFactor=1.1,
                minNeighbors=3,
                minSize=(50, 50),
            )
            for (px, py, pw, ph) in profile_faces_flip:
                mapped_x = w_img - int(px) - int(pw)
                candidates.append({
                    'box': (mapped_x, int(py), int(pw), int(ph)),
                    'source': 'haar_profile_flipped',
                    'pose_hint': 'left',
                })
        except Exception:
            pass

        # 3) MediaPipe face-detection fallback
        try:
            import mediapipe as mp
            rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            with mp.solutions.face_detection.FaceDetection(
                model_selection=0,
                min_detection_confidence=0.4,
            ) as face_detector:
                fd_result = face_detector.process(rgb_img)

            if fd_result and fd_result.detections:
                for det in fd_result.detections:
                    bbox = det.location_data.relative_bounding_box
                    bx = max(0, int(bbox.xmin * w_img))
                    by = max(0, int(bbox.ymin * h_img))
                    bw = int(bbox.width * w_img)
                    bh = int(bbox.height * h_img)
                    if bw > 0 and bh > 0:
                        candidates.append({
                            'box': (bx, by, bw, bh),
                            'source': 'mediapipe_fd',
                            'pose_hint': 'unknown',
                        })
        except Exception:
            pass

        if len(candidates) == 0:
            return Response({
                'success': True,
                'passed': False,
                'issues': ['No clear face detected. Keep one face visible, turn less extreme, and ensure your face is well lit.'],
                'expected_pose': expected_pose,
                'detected_pose': 'unknown',
            })

        best = max(candidates, key=lambda c: int(c['box'][2]) * int(c['box'][3]))
        x, y, w, h = best['box']
        detection_source = best.get('source', 'unknown')
        pose_hint = best.get('pose_hint', 'unknown')

        # Estimate head yaw using facial landmarks so side-pose checks require
        # actual head rotation (ear-visible profile), not just horizontal shift.
        detected_pose = pose_hint if pose_hint in {'left', 'right', 'straight'} else 'straight'
        yaw_norm = 0.0
        pose_method = 'bbox_fallback'

        try:
            import mediapipe as mp
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            with mp.solutions.face_mesh.FaceMesh(
                static_image_mode=True,
                max_num_faces=1,
                refine_landmarks=False,
                min_detection_confidence=0.5,
            ) as face_mesh:
                mesh_result = face_mesh.process(rgb)

            if mesh_result and mesh_result.multi_face_landmarks:
                landmarks = mesh_result.multi_face_landmarks[0].landmark

                # Canonical FaceMesh landmarks: nose tip=1, left cheek=234, right cheek=454
                nose_x = float(landmarks[1].x)
                left_x = float(landmarks[234].x)
                right_x = float(landmarks[454].x)
                face_width = max(1e-4, right_x - left_x)
                face_mid_x = (left_x + right_x) / 2.0
                yaw_norm = (nose_x - face_mid_x) / face_width
                pose_method = 'face_mesh_yaw'

                # Sign convention (camera view):
                # yaw_norm > 0  -> student turned to their LEFT
                # yaw_norm < 0  -> student turned to their RIGHT
                if yaw_norm >= 0.10:
                    detected_pose = 'left'
                elif yaw_norm <= -0.10:
                    detected_pose = 'right'
                else:
                    detected_pose = 'straight'
            else:
                # Conservative fallback if landmarks are unavailable.
                face_center_x = x + (w / 2.0)
                center_ratio = face_center_x / float(max(1, w_img))
                if center_ratio < 0.33:
                    detected_pose = 'left'
                elif center_ratio > 0.67:
                    detected_pose = 'right'
                else:
                    detected_pose = 'straight'
                pose_method = 'bbox_fallback'
        except Exception:
            # Keep fallback path robust even if MediaPipe fails at runtime.
            face_center_x = x + (w / 2.0)
            center_ratio = face_center_x / float(max(1, w_img))
            if center_ratio < 0.33:
                detected_pose = 'left'
            elif center_ratio > 0.67:
                detected_pose = 'right'
            else:
                detected_pose = 'straight'
            pose_method = 'bbox_fallback'

        # Prefer explicit profile hints when face mesh yaw is unavailable.
        if pose_method != 'face_mesh_yaw' and pose_hint in {'left', 'right'}:
            detected_pose = pose_hint
            pose_method = 'profile_hint'

        face_roi = gray[y:y + h, x:x + w]
        blur_score = float(cv2.Laplacian(face_roi, cv2.CV_64F).var()) if face_roi.size else 0.0
        brightness = float(np.mean(face_roi)) if face_roi.size else 0.0
        face_area_ratio = float((w * h) / float(max(1, w_img * h_img)))

        # Keep straight-pose guidance lenient for registration usability.
        if expected_pose == 'straight':
            pose_ok = abs(yaw_norm) <= 0.18 if pose_method == 'face_mesh_yaw' else True

        size_ok = face_area_ratio >= 0.035
        blur_ok = blur_score >= 20.0
        light_ok = 25.0 <= brightness <= 240.0

        issues = []
        if not pose_ok:
            issues.append(
                f"Pose mismatch: expected STRAIGHT, detected {detected_pose.upper()}. Keep your face centered and look directly at camera."
            )
        if not size_ok:
            issues.append('Face is too small. Move closer to camera.')
        if not blur_ok:
            issues.append('Image is blurry. Hold still and recapture.')
        if not light_ok:
            issues.append('Lighting is poor. Ensure your face is clearly lit.')

        passed = pose_ok and size_ok and blur_ok and light_ok

        return Response({
            'success': True,
            'passed': passed,
            'expected_pose': expected_pose,
            'detected_pose': detected_pose,
            'metrics': {
                'blur_score': round(blur_score, 2),
                'brightness': round(brightness, 2),
                'face_area_ratio': round(face_area_ratio, 4),
                'yaw_norm': round(float(yaw_norm), 4),
                'pose_method': pose_method,
                'detection_source': detection_source,
            },
            'issues': issues,
            'message': 'Capture accepted' if passed else 'Please adjust and try again',
        })
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


# ─── Session Endpoints ────────────────────────────────────────────────────────

@api_view(['GET'])
def list_sessions(request):
    try:
        sessions = ClassSession.objects.all()[:20]
        return Response({'sessions': [{
            'id': s.id, 'class_name': s.class_name, 'subject': s.subject,
            'start_time': s.start_time.isoformat(),
            'end_time': s.end_time.isoformat() if s.end_time else None,
            'status': s.status, 'duration_minutes': s.duration_minutes,
            'total_students': s.total_students,
        } for s in sessions]})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@csrf_exempt
def start_session(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        import json
        data = json.loads(request.body) if request.body else {}
        class_name = data.get('class_name', 'CS101')
        subject = data.get('subject', 'Computer Science')
        unit = str(data.get('unit') or '').strip()
        topic_name = str(data.get('topic_name') or data.get('topic') or '').strip()
        daily_plan_id_raw = data.get('daily_plan_id')
        lecture_plan_id_raw = data.get('lecture_plan_id')
        camera_source = data.get('camera_source', '0')
        teacher_id_raw = data.get('teacher_id', 1)
        teacher_id = 1
        try:
            teacher_id = int(str(teacher_id_raw).replace('teacher', ''))
        except (ValueError, TypeError):
            teacher_id = 1

        teacher = Teacher.objects.filter(id=teacher_id).first()
        if not teacher:
            teacher, _ = Teacher.objects.get_or_create(
                email='demo@smartclass.com',
                defaults={'name': 'Demo Teacher', 'password_hash': hash_password('demo123')}
            )

        daily_plan = None
        lecture_plan = None
        if lecture_plan_id_raw not in (None, '', 'null'):
            try:
                lecture_plan = (
                    LecturePlan.objects
                    .select_related('topic')
                    .get(id=int(lecture_plan_id_raw), teacher=teacher)
                )
                subject = lecture_plan.topic.subject
                unit = lecture_plan.topic.unit
                topic_name = lecture_plan.topic.topic
            except Exception as plan_error:
                logger.warning(f"Invalid lecture_plan_id '{lecture_plan_id_raw}' at session start: {plan_error}")
                lecture_plan = None

        if daily_plan_id_raw not in (None, '', 'null'):
            try:
                daily_plan = (
                    DailyLectureTopic.objects
                    .select_related('topic')
                    .get(id=int(daily_plan_id_raw), topic__teacher=teacher)
                )
                subject = daily_plan.topic.subject
                unit = daily_plan.topic.unit
                topic_name = daily_plan.topic.topic
            except Exception as plan_error:
                logger.warning(f"Invalid daily_plan_id '{daily_plan_id_raw}' at session start: {plan_error}")
                daily_plan = None

        active_sessions = list(ClassSession.objects.filter(status='active'))
        for active_session in active_sessions:
            _finalize_session(active_session)

        session = ClassSession.objects.create(
            teacher=teacher, class_name=class_name, subject=subject,
            camera_source=camera_source,
            total_students=Student.objects.filter(is_active=True).count(),
        )

        from .video_stream import start_stream, stop_stream

        # Reset any stale stream instance before starting a new session.
        try:
            stop_stream()
        except Exception:
            pass

        cam_source = int(str(camera_source)) if str(camera_source).isdigit() else 0
        camera_candidates = [cam_source, 0, 1, 2]
        # Keep unique order while preserving preferred source first.
        camera_candidates = list(dict.fromkeys(camera_candidates))

        started = False
        selected_source = cam_source
        for source_idx in camera_candidates:
            started = start_stream(source=source_idx, session_id=session.id)
            if started:
                selected_source = source_idx
                break
            # Ensure failed attempt is fully released before next source.
            try:
                stop_stream()
            except Exception:
                pass

        from .video_stream import get_video_stream
        stream = get_video_stream()
        camera_started = bool(started or stream.is_running)
        session.camera_source = str(selected_source)
        session.save(update_fields=['camera_source'])

        today = timezone.now().date()
        for student in Student.objects.filter(is_active=True):
            Attendance.objects.get_or_create(
                student=student, session=session, date=today,
                defaults={'is_present': False}
            )

        _active_session_topic_map[session.id] = {
            'daily_plan_id': daily_plan.id if daily_plan else (int(daily_plan_id_raw) if str(daily_plan_id_raw).isdigit() else None),
            'lecture_plan_id': lecture_plan.id if lecture_plan else (int(lecture_plan_id_raw) if str(lecture_plan_id_raw).isdigit() else None),
            'subject': subject,
            'unit': unit,
            'topic_name': topic_name,
        }

        return JsonResponse({'success': True, 'session': {
            'id': session.id, 'class_name': session.class_name,
            'subject': subject,
            'unit': unit,
            'topic_name': topic_name,
            'daily_plan_id': _active_session_topic_map[session.id].get('daily_plan_id'),
            'lecture_plan_id': _active_session_topic_map[session.id].get('lecture_plan_id'),
            'start_time': session.start_time.isoformat(),
            'camera_started': camera_started,
            'camera_source': str(selected_source),
        }})
    except Exception as e:
        logger.error(f"Start session error: {e}")
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def end_session(request, session_id):
    try:
        logger.info(f"🛑 Ending session {session_id}")
        
        session = ClassSession.objects.get(id=session_id)

        # Idempotency: if session was already ended, just return existing auto-report state.
        if session.status == 'ended' and session.end_time:
            auto_report = _finalize_session(session)
            return Response({
                'success': True,
                'duration': session.duration_minutes,
                'stream_stopped': True,
                'topic_updated': True,
                'already_ended': True,
                'auto_report_generated': bool(auto_report and auto_report.status == 'completed'),
                'auto_report_id': auto_report.id if auto_report else None,
            })

        topic_ctx = _active_session_topic_map.get(session.id, {})
        topic_updated = bool(topic_ctx.get('daily_plan_id') or topic_ctx.get('lecture_plan_id'))
        
        # Suppress live_data auto-restart briefly while session is being finalized.
        global _AUTO_RESTART_SUPPRESS_UNTIL
        _AUTO_RESTART_SUPPRESS_UNTIL = timezone.now() + timedelta(seconds=20)

        # Stop the video stream immediately
        from .video_stream import stop_stream
        stream_stopped = stop_stream()
        
        if stream_stopped:
            logger.info(f"✅ Session {session_id} ended successfully - Stream stopped")
        else:
            logger.warning(f"⚠️ Session {session_id} ended but stream stop failed")

        auto_report = _finalize_session(session)
        
        return Response({
            'success': True, 
            'duration': session.duration_minutes,
            'stream_stopped': stream_stopped,
            'topic_updated': topic_updated,
            'auto_report_generated': bool(auto_report and auto_report.status == 'completed'),
            'auto_report_id': auto_report.id if auto_report else None,
        })
        
        
    except ClassSession.DoesNotExist:
        logger.error(f"❌ Session {session_id} not found")
        return Response({'error': 'Session not found'}, status=404)
    except Exception as e:
        logger.error(f"❌ Error ending session {session_id}: {e}")
        return Response({'error': str(e)}, status=500)


def _generate_session_auto_report(session):
    """Create a CSV report automatically whenever a session is ended."""
    report = None
    try:
        import os
        import pandas as pd
        from django.conf import settings

        session_end = session.end_time or timezone.now()
        report_name = f"Session Report - {session.class_name} - {session.start_time.strftime('%Y-%m-%d %H-%M')}"

        # Idempotency: if this session auto-report already exists, reuse it.
        existing = (
            Report.objects
            .filter(name=report_name, report_type='summary')
            .order_by('-created_at')
            .first()
        )
        if existing:
            return existing

        report = Report.objects.create(
            name=report_name,
            report_type='summary',
            format='csv',
            status='generating',
            date_from=session.start_time.date(),
            date_to=session_end.date(),
        )

        engagement_qs = EngagementRecord.objects.filter(session=session).select_related('student').order_by('student__student_id', 'timestamp')
        attendance_qs = Attendance.objects.filter(session=session).select_related('student')

        attendance_by_sid = {}
        for a in attendance_qs:
            attendance_by_sid[a.student.student_id] = a

        student_rows = []
        if engagement_qs.exists():
            from collections import defaultdict

            by_sid = defaultdict(list)
            for rec in engagement_qs:
                by_sid[rec.student.student_id].append(rec)

            for sid, rows in by_sid.items():
                first = rows[0]
                student_name = first.student.name
                avg_eng = sum(float(r.engagement_score or 0.0) for r in rows) / max(len(rows), 1)
                avg_att = sum(float(r.attention_score or 0.0) for r in rows) / max(len(rows), 1)

                emotion_counts = {}
                for r in rows:
                    emotion = str(r.emotion or 'unknown')
                    emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
                dominant_emotion = max(emotion_counts, key=emotion_counts.get) if emotion_counts else 'unknown'

                attendance = attendance_by_sid.get(sid)
                student_rows.append({
                    'Session ID': session.id,
                    'Class Name': session.class_name,
                    'Subject': session.subject,
                    'Student ID': sid,
                    'Student Name': student_name,
                    'Attendance': 'Present' if (attendance and attendance.is_present) else 'Absent',
                    'Avg Engagement Score': round(avg_eng, 1),
                    'Avg Attention Score': round(avg_att, 1),
                    'Dominant Emotion': dominant_emotion,
                    'Samples Captured': len(rows),
                    'Session Start': session.start_time.isoformat(),
                    'Session End': session_end.isoformat(),
                    'Duration (min)': session.duration_minutes,
                })
        else:
            # Still produce a session-level report row even if no detections were captured.
            student_rows.append({
                'Session ID': session.id,
                'Class Name': session.class_name,
                'Subject': session.subject,
                'Student ID': '',
                'Student Name': 'No engagement records captured',
                'Attendance': '',
                'Avg Engagement Score': 0.0,
                'Avg Attention Score': 0.0,
                'Dominant Emotion': 'unknown',
                'Samples Captured': 0,
                'Session Start': session.start_time.isoformat(),
                'Session End': session_end.isoformat(),
                'Duration (min)': session.duration_minutes,
            })

        df = pd.DataFrame(student_rows)

        reports_dir = os.path.join(settings.MEDIA_ROOT, 'reports')
        os.makedirs(reports_dir, exist_ok=True)

        filename = f"session_report_{session.id}_{report.id}.csv"
        file_path = os.path.join(reports_dir, filename)
        df.to_csv(file_path, index=False)

        report.file_path = f"reports/{filename}"
        report.status = 'completed'
        report.generated_at = timezone.now()
        report.file_size = os.path.getsize(file_path)
        report.save(update_fields=['file_path', 'status', 'generated_at', 'file_size'])
        return report
    except Exception as e:
        logger.warning(f"Auto report generation failed for session {session.id}: {e}")
        if report is not None:
            report.status = 'failed'
            report.save(update_fields=['status'])
        return None


def _finalize_session(session):
    """Mark session ended, update planned topic state, and auto-generate report."""
    if session.status == 'ended' and session.end_time:
        return _generate_session_auto_report(session)

    session.status = 'ended'
    session.end_time = timezone.now()
    session.save(update_fields=['status', 'end_time'])

    topic_ctx = _active_session_topic_map.get(session.id, {})
    daily_plan_id = topic_ctx.get('daily_plan_id')
    lecture_plan_id = topic_ctx.get('lecture_plan_id')
    if daily_plan_id:
        try:
            plan = DailyLectureTopic.objects.select_related('topic').get(id=daily_plan_id)
            if not plan.is_completed:
                plan.is_completed = True
                plan.completed_at = timezone.now()
                plan.save(update_fields=['is_completed', 'completed_at'])

            topic = plan.topic
            if topic.status != 'completed':
                topic.status = 'completed'
                topic.checkpoint_assigned = True
                topic.checkpoint_completion_rate = max(float(topic.checkpoint_completion_rate or 0.0), 75.0)
                topic.save(update_fields=['status', 'checkpoint_assigned', 'checkpoint_completion_rate'])
        except Exception as topic_close_error:
            logger.warning(f"Failed to update selected daily topic on session end: {topic_close_error}")

    if lecture_plan_id:
        try:
            lecture_plan = LecturePlan.objects.select_related('topic').get(id=lecture_plan_id)
            if lecture_plan.status != 'done':
                lecture_plan.status = 'done'
                lecture_plan.save(update_fields=['status'])

            topic = lecture_plan.topic
            if topic.status != 'completed':
                topic.status = 'completed'
                topic.save(update_fields=['status'])
        except Exception as lecture_close_error:
            logger.warning(f"Failed to update selected lecture plan on session end: {lecture_close_error}")

    _active_session_topic_map.pop(session.id, None)
    return _generate_session_auto_report(session)


def _ensure_missing_session_reports(limit=50):
    """Backfill missing auto reports for ended sessions shown on the reports page."""
    ended_sessions = ClassSession.objects.filter(status='ended').order_by('-start_time')[:limit]
    for session in ended_sessions:
        expected_name = f"Session Report - {session.class_name} - {session.start_time.strftime('%Y-%m-%d %H-%M')}"
        exists = Report.objects.filter(name=expected_name, report_type='summary').exists()
        if not exists:
            _generate_session_auto_report(session)


def _behavior_student_key(student_item, face_idx):
    sid = student_item.get('student_id')
    if sid:
        return f"sid:{sid}"
    return f"face:{face_idx}"


def _compute_behavior_flags(student_item):
    emotion = str(student_item.get('emotion') or 'neutral').lower()
    engagement = float(
        student_item.get('engagement_score')
        or student_item.get('engagement')
        or 0.0
    )
    emotion_conf = float(student_item.get('emotion_confidence') or 0.0)
    face_conf = float(student_item.get('confidence') or 0.0)
    is_looking_forward = bool(student_item.get('is_looking_forward'))
    head_direction_score = float(student_item.get('head_direction_score') or 0.0)
    eye_contact_score = float(student_item.get('eye_contact_score') or 0.0)
    vertical_offset = float(student_item.get('vertical_offset') or 0.0)
    horizontal_offset = float(student_item.get('horizontal_offset') or 0.0)

    looking_down = not is_looking_forward
    likely_occluded = (face_conf < 0.35 and emotion_conf < 0.45) or (face_conf < 0.25)
    likely_note_taking = (
        looking_down
        and (not likely_occluded)
        and emotion in {'neutral', 'focused', 'happy'}
        and engagement >= 58
        and (head_direction_score >= 42 or eye_contact_score >= 40)
    )

    # Looking-away proxy (e.g., up/up-right) when not forward, not occluded,
    # and not likely writing notes.
    likely_looking_away = (
        looking_down
        and (not likely_occluded)
        and (not likely_note_taking)
        and emotion in {'neutral', 'focused', 'happy', 'confused'}
        and (engagement >= 40 or eye_contact_score >= 30)
        and (
            head_direction_score < 55
            or vertical_offset > 0.08
            or abs(horizontal_offset) > 0.18
        )
    )

    return {
        'emotion': emotion,
        'engagement': engagement,
        'looking_down': looking_down,
        'likely_occluded': likely_occluded,
        'likely_note_taking': likely_note_taking,
        'likely_looking_away': likely_looking_away,
        'head_direction_score': head_direction_score,
        'eye_contact_score': eye_contact_score,
    }


def _update_behavior_state(student_key, flags):
    state = _student_behavior_state.get(student_key, {
        'down_streak': 0,
        'hidden_streak': 0,
        'notes_streak': 0,
        'away_streak': 0,
        'last_seen': timezone.now(),
    })

    if flags['looking_down']:
        state['down_streak'] += 1
    else:
        state['down_streak'] = 0

    if flags['looking_down'] and flags['likely_occluded']:
        state['hidden_streak'] += 1
    else:
        state['hidden_streak'] = 0

    if flags['likely_note_taking']:
        state['notes_streak'] += 1
    else:
        state['notes_streak'] = 0

    if flags.get('likely_looking_away'):
        state['away_streak'] += 1
    else:
        state['away_streak'] = 0

    state['last_seen'] = timezone.now()
    _student_behavior_state[student_key] = state
    return state


def _can_emit_behavior_alert(student_key, alert_type, now):
    key = f"{student_key}:{alert_type}"
    last = _last_behavior_alert_at.get(key)
    if last and (now - last).total_seconds() < _BEHAVIOR_ALERT_COOLDOWN_SECONDS:
        return False
    _last_behavior_alert_at[key] = now
    return True


def _cleanup_behavior_state(now, ttl_seconds=90):
    stale_keys = []
    for key, state in _student_behavior_state.items():
        last_seen = state.get('last_seen', now)
        if (now - last_seen).total_seconds() > ttl_seconds:
            stale_keys.append(key)

    for key in stale_keys:
        _student_behavior_state.pop(key, None)


# ─── Live Monitoring Endpoints ────────────────────────────────────────────────

@api_view(['GET'])
def video_feed(request):
    try:
        from .video_stream import get_video_stream, generate_mjpeg_frames
        stream = get_video_stream()

        # Do not auto-start camera from a read endpoint.
        # Stream must be started explicitly via session/camera start APIs.
        if stream.is_running:
            response = StreamingHttpResponse(
                generate_mjpeg_frames(),
                content_type='multipart/x-mixed-replace; boundary=frame'
            )
            response['Cache-Control'] = 'no-cache'
            return response
        else:
            return HttpResponse("Video stream is not active. Start monitoring first.", status=409)
    except Exception as e:
        logger.error(f"Stream error: {e}")
        return HttpResponse(f"Stream error: {e}", status=500)

@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def stop_stream_force(request):
    try:
        global _AUTO_RESTART_SUPPRESS_UNTIL
        _AUTO_RESTART_SUPPRESS_UNTIL = timezone.now() + timedelta(seconds=20)
        from .video_stream import stop_stream
        stopped = stop_stream()
        return Response({'success': True, 'stream_stopped': stopped})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def live_data(request):
    try:
        from .video_stream import get_video_stream
        stream = get_video_stream()
        stream_status = stream.get_status()
        active_session = ClassSession.objects.filter(status='active').first()

        analysis = stream.get_latest_analysis()
        now = timezone.now()
        _cleanup_behavior_state(now)

        # If live monitoring is running without an explicit session start,
        # create a lightweight active session so attendance can be recorded.
        if active_session is None and stream_status.get('is_running'):
            teacher = Teacher.objects.filter(is_active=True).first()
            if teacher is None:
                teacher = Teacher.objects.create(
                    name='Demo Teacher',
                    email='demo@smartclass.com',
                    password_hash=hash_password('demo123'),
                    subject='Computer Science',
                )

            active_session = ClassSession.objects.create(
                teacher=teacher,
                class_name='Live Class',
                subject='Computer Science',
                camera_source='0',
                total_students=Student.objects.filter(is_active=True).count(),
                status='active',
            )

            auto_plan = (
                DailyLectureTopic.objects
                .select_related('topic')
                .filter(topic__teacher=teacher, lecture_date=timezone.now().date())
                .order_by('id')
                .first()
            )
            _active_session_topic_map[active_session.id] = {
                'daily_plan_id': auto_plan.id if auto_plan else None,
                'subject': auto_plan.topic.subject if auto_plan else active_session.subject,
                'unit': auto_plan.topic.unit if auto_plan else '',
                'topic_name': auto_plan.topic.topic if auto_plan else '',
            }

            today = timezone.now().date()
            for student in Student.objects.filter(is_active=True):
                Attendance.objects.get_or_create(
                    student=student,
                    session=active_session,
                    date=today,
                    defaults={'is_present': False},
                )

        recent_snapshots = ClassEngagementSnapshot.objects.filter(
            timestamp__gte=timezone.now() - timedelta(minutes=5)
        ).order_by('-timestamp')[:6]
        timeline = [{
            'time': timezone.localtime(s.timestamp).strftime('%H:%M:%S'),
            'engagement': round(s.avg_engagement, 1),
            'present': s.present_count,
        } for s in reversed(list(recent_snapshots))]

        # Generate real-time alerts from all currently detected faces/students.
        alerts_data = []
        detected_students = []

        if analysis:
            detected_students = list(analysis.get('students') or [])
            if not detected_students and analysis.get('recognized_students'):
                # Compatibility fallback: convert recognized payload to common fields.
                for item in analysis.get('recognized_students', []):
                    detected_students.append({
                        'face_index': item.get('face_index'),
                        'student_id': item.get('student_id'),
                        'name': item.get('name'),
                        'emotion': item.get('emotion'),
                        'engagement_score': item.get('engagement'),
                        'confidence': item.get('confidence'),
                        'emotion_confidence': item.get('emotion_confidence'),
                        'is_looking_forward': item.get('is_looking_forward'),
                        'head_direction_score': item.get('head_direction_score'),
                        'eye_contact_score': item.get('eye_contact_score'),
                        'vertical_offset': item.get('vertical_offset'),
                        'horizontal_offset': item.get('horizontal_offset'),
                        'face_registered': bool(item.get('student_id')),
                    })

        if analysis and active_session and analysis.get('recognized_students'):
            try:
                _mark_attendance_from_face_detections(active_session, analysis.get('recognized_students', []))
            except Exception as attendance_error:
                logger.warning(f"Attendance update from face detection failed: {attendance_error}")

        if analysis and active_session:
            try:
                _persist_live_engagement_records(active_session, analysis, now)
            except Exception as persist_error:
                logger.warning(f"Live engagement persistence failed: {persist_error}")

        if analysis and detected_students:
            for idx, student_data in enumerate(detected_students, start=1):
                student_name = student_data.get('name') or student_data.get('student_name') or f"Detected Face {idx}"
                student_id = student_data.get('student_id')
                behavior_flags = _compute_behavior_flags(student_data)
                student_key = _behavior_student_key(student_data, idx)
                state = _update_behavior_state(student_key, behavior_flags)

                # Enrich outgoing student payload so frontend can explain why alert fired.
                student_data['behavior_state'] = {
                    'looking_down': behavior_flags['looking_down'],
                    'likely_occluded': behavior_flags['likely_occluded'],
                    'likely_note_taking': behavior_flags['likely_note_taking'],
                    'likely_looking_away': behavior_flags.get('likely_looking_away', False),
                    'down_streak': state['down_streak'],
                    'hidden_streak': state['hidden_streak'],
                    'notes_streak': state['notes_streak'],
                    'away_streak': state.get('away_streak', 0),
                }

                # Hidden-face alert: sustained down-looking with likely occlusion.
                if state['hidden_streak'] >= 2 and _can_emit_behavior_alert(student_key, 'hidden_face', now):
                    alerts_data.append({
                        'id': f"hidden_face_{student_key}_{int(time.time())}",
                        'type': 'hidden_face',
                        'severity': 'high',
                        'message': f"{student_name} may be hiding face while looking down.",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': now.strftime('%H:%M:%S'),
                        'timestamp': now.isoformat(),
                    })
                    continue

                # Note-taking inference: do not trigger low-attention alert in this case.
                if state['notes_streak'] >= 2 and _can_emit_behavior_alert(student_key, 'note_taking', now):
                    alerts_data.append({
                        'id': f"notes_{student_key}_{int(time.time())}",
                        'type': 'note_taking',
                        'severity': 'info',
                        'message': f"{student_name} appears to be writing notes (down-looking but attentive).",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': now.strftime('%H:%M:%S'),
                        'timestamp': now.isoformat(),
                    })
                    continue

                # Looking-away behavior (up/up-right proxy) with name-specific message.
                if state.get('away_streak', 0) >= 2 and _can_emit_behavior_alert(student_key, 'looking_away', now):
                    alerts_data.append({
                        'id': f"away_{student_key}_{int(time.time())}",
                        'type': 'looking_away',
                        'severity': 'medium',
                        'message': f"{student_name} appears to be looking up/up-right and away from class focus.",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': now.strftime('%H:%M:%S'),
                        'timestamp': now.isoformat(),
                    })
                    continue

                # Sustained disengagement when looking down without note-taking evidence.
                if (
                    state['down_streak'] >= 3
                    and state['notes_streak'] == 0
                    and behavior_flags['engagement'] < 50
                    and _can_emit_behavior_alert(student_key, 'low_attention', now)
                ):
                    alerts_data.append({
                        'id': f"low_attention_{student_key}_{int(time.time())}",
                        'type': 'low_attention',
                        'severity': 'medium',
                        'message': f"{student_name} is looking down for a long duration with low engagement.",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': now.strftime('%H:%M:%S'),
                        'timestamp': now.isoformat(),
                    })

        # Class-level low engagement alert (persisted to DB).
        class_avg_engagement = 0
        class_present_count = 0
        if analysis:
            class_avg_engagement = analysis.get('class_avg_engagement', analysis.get('avg_engagement', 0)) or 0
            class_present_count = int(
                analysis.get('present_count')
                or analysis.get('faces_detected')
                or len(analysis.get('students') or [])
                or 0
            )

        if class_present_count > 0 and class_avg_engagement > 0 and class_avg_engagement < 30:
            class_alert_msg = f"Class average engagement is low ({round(class_avg_engagement, 1)}%). Immediate intervention recommended."

            # Always surface on live panel.
            alerts_data.append({
                'id': f"class_low_{int(time.time())}",
                'type': 'low_engagement',
                'severity': 'high',
                'message': class_alert_msg,
                'student_name': 'Classroom',
                'student_id': None,
                'time': now.strftime('%H:%M:%S'),
                'timestamp': now.isoformat(),
            })

            # Persist to DB if an active session exists, with short cooldown to avoid spam.
            if active_session:
                recent_duplicate = Alert.objects.filter(
                    session=active_session,
                    student__isnull=True,
                    alert_type='low_engagement',
                    is_resolved=False,
                    timestamp__gte=now - timedelta(minutes=2),
                ).exists()

                if not recent_duplicate:
                    Alert.objects.create(
                        session=active_session,
                        student=None,
                        alert_type='low_engagement',
                        severity='high',
                        message=class_alert_msg,
                    )
        elif active_session:
            # Class is not currently in a low-engagement state.
            # Auto-resolve previous unresolved class-level low engagement alerts
            # so stale "0%" messages do not remain visible.
            Alert.objects.filter(
                session=active_session,
                student__isnull=True,
                alert_type='low_engagement',
                is_resolved=False,
            ).update(is_resolved=True, resolved_at=now)

        # Include unresolved DB alerts so Live Alerts box always reflects persisted records.
        if active_session:
            db_alerts = Alert.objects.filter(
                session=active_session,
                is_resolved=False,
            ).order_by('-timestamp')[:10]

            for db_alert in db_alerts:
                # Surface low-engagement DB alerts only when class is currently low.
                if (
                    db_alert.alert_type == 'low_engagement'
                    and not (class_present_count > 0 and class_avg_engagement > 0 and class_avg_engagement < 30)
                ):
                    continue
                alerts_data.append({
                    'id': f"db_{db_alert.id}",
                    'type': db_alert.alert_type,
                    'severity': db_alert.severity,
                    'message': db_alert.message,
                    'student_name': db_alert.student.name if db_alert.student else 'Classroom',
                    'student_id': db_alert.student.student_id if db_alert.student else None,
                    'time': timezone.localtime(db_alert.timestamp).strftime('%H:%M:%S'),
                    'timestamp': db_alert.timestamp.isoformat(),
                })

        alerts_data.sort(
            key=lambda x: x.get('timestamp', x.get('time', '')),
            reverse=True,
        )
        alerts_data = alerts_data[:10]

        if analysis:
            # Prepare students data - SHOW ALL DETECTED STUDENTS
            students_data = []
            for i, student_data in enumerate(detected_students, start=1):
                sid = student_data.get('student_id')
                students_data.append({
                    'student_id': sid or student_data.get('face_index') or f"FACE_{i}",
                    'name': student_data.get('student_name') or student_data.get('name') or f"Detected Face {i}",
                    'engagement_score': student_data.get('engagement_score', student_data.get('engagement', 0)),
                    'emotion': student_data.get('emotion', 'neutral'),
                    'confidence': student_data.get('confidence', student_data.get('emotion_confidence', 0)),
                    'present_today': True,
                    'face_registered': bool(sid),
                    'is_looking_forward': bool(student_data.get('is_looking_forward')),
                    'behavior_state': student_data.get('behavior_state', {}),
                })

            return Response({
                'stream_active': stream_status['is_running'],
                'fps': stream_status['fps'],
                'session_id': active_session.id if active_session else None,
                'session': {
                    'class_name': active_session.class_name if active_session else 'Live Class',
                    'subject': (_active_session_topic_map.get(active_session.id, {}) if active_session else {}).get('subject') or (active_session.subject if active_session else 'Computer Science'),
                    'unit': (_active_session_topic_map.get(active_session.id, {}) if active_session else {}).get('unit') or '',
                    'topic_name': (_active_session_topic_map.get(active_session.id, {}) if active_session else {}).get('topic_name') or '',
                    'daily_plan_id': (_active_session_topic_map.get(active_session.id, {}) if active_session else {}).get('daily_plan_id'),
                    'teacher_id': active_session.teacher.id if active_session and active_session.teacher else None,
                    'teacher_name': active_session.teacher.name if active_session and active_session.teacher else 'Teacher',
                    'start_time': active_session.start_time.isoformat() if active_session else None,
                } if active_session else None,
                'present_count': analysis.get('present_count', len([s for s in students_data if s.get('present_today', True)])),
                'avg_engagement': class_avg_engagement,
                'fusion_enabled': analysis.get('fusion_enabled', False),
                'fusion_weights': analysis.get('fusion_weights', {'fer': 0.0, 'daisee': 0.0}),
                'daisee_model_loaded': analysis.get('daisee_model_loaded', False),
                'emotion_distribution': analysis.get('emotion_distribution', {}),
                'students': students_data[:12],
                'recognized_students': analysis.get('recognized_students', []),
                'timeline': timeline,
                'alerts': alerts_data,
                'timestamp': analysis.get('timestamp'),
            })
        else:
            return Response(_generate_demo_live_data(active_session, timeline, alerts_data))
    except Exception as e:
        logger.error(f"Live data error: {e}")
        return Response({'error': str(e), 'stream_active': False}, status=500)


def _generate_demo_live_data(active_session, timeline, alerts_data):
    import random
    emotions = ['happy', 'neutral', 'confused', 'bored', 'focused']
    students_demo = [{
        'face_index': i,
        'emotion': random.choice(emotions),
        'emotion_confidence': round(random.uniform(0.6, 0.95), 2),
        'engagement_score': random.randint(50, 95),
        'attention_score': random.randint(60, 95),
        'posture_score': random.randint(70, 95),
        'is_looking_forward': random.random() > 0.3,
        'face_detected': True,
    } for i in range(8)]
    avg_eng = sum(s['engagement_score'] for s in students_demo) / len(students_demo)
    return {
        'stream_active': False, 'fps': 0,
        'session_id': active_session.id if active_session else None,
        'present_count': 8, 'avg_engagement': round(avg_eng, 1),
        'emotion_distribution': {'happy': 3, 'neutral': 2, 'confused': 2, 'bored': 1},
        'students': students_demo, 'timeline': timeline, 'alerts': alerts_data,
        'timestamp': timezone.now().isoformat(), 'demo_mode': True,
    }


@api_view(['GET'])
def stream_frame(request):
    try:
        from .video_stream import get_video_stream
        stream = get_video_stream()
        if not stream.is_running:
            return Response({'error': 'Stream not active', 'frame': None})
        frame_b64 = stream.get_frame_base64()
        return Response({'frame': frame_b64, 'timestamp': time.time()})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Alerts ───────────────────────────────────────────────────────────────────



# ─── Alert System ───────────────────────────────────────────────────────────

@api_view(['GET'])
def check_engagement_alert(request):
    """Check engagement data and trigger alerts if confusion > 30%"""
    try:
        from django.utils import timezone
        from datetime import timedelta
        
        # Get latest engagement data from last 5 minutes
        five_minutes_ago = timezone.now() - timedelta(minutes=5)
        
        # Get latest engagement records per student (SQLite compatible)
        latest_records = []
        
        # Get all students with recent engagement data
        student_ids = EngagementRecord.objects.filter(
            timestamp__gte=five_minutes_ago,
            face_detected=True
        ).values_list('student_id', flat=True).distinct()
        
        # For each student, get their latest record
        for student_id in student_ids:
            latest_record = EngagementRecord.objects.filter(
                student_id=student_id,
                timestamp__gte=five_minutes_ago,
                face_detected=True
            ).order_by('-timestamp').first()
            
            if latest_record:
                latest_records.append(latest_record)
        
        if not latest_records:
            return Response({
                'alert': False,
                'percentage': 0,
                'message': 'No engagement data available',
                'total_students': 0,
                'confused_students': 0
            })
        
        total_students = len(latest_records)
        confused_students = 0
        
        for record in latest_records:
            # Student is confused if emotion = confused OR engagement_score < 0.4
            if record.emotion == 'confused' or record.engagement_score < 40.0:
                confused_students += 1
        
        percentage_confused = (confused_students / total_students) * 100 if total_students > 0 else 0
        
        # Check if alert should be triggered
        alert_triggered = percentage_confused > 30.0
        
        if alert_triggered:
            # Create alert in database
            active_session = ClassSession.objects.filter(status='active').first()
            
            if active_session:
                Alert.objects.create(
                    session=active_session,
                    alert_type='class_confusion',
                    severity='high' if percentage_confused > 50 else 'medium',
                    message=f'⚠ ALERT: More than 30% students appear confused or not engaged ({percentage_confused:.1f}%).',
                    timestamp=timezone.now()
                )
                _create_notification_if_needed(
                    'alert',
                    f'Class confusion alert: {percentage_confused:.1f}% students appear confused.',
                    related_student=None,
                    dedupe_hours=2,
                )
        
        return Response({
            'alert': alert_triggered,
            'percentage': round(percentage_confused, 1),
            'message': f'⚠ ALERT: More than 30% students appear confused ({percentage_confused:.1f}%).' if alert_triggered else 'Class engagement is normal',
            'total_students': total_students,
            'confused_students': confused_students,
            'timestamp': timezone.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error checking engagement alert: {e}")
        return Response({
            'error': str(e),
            'alert': False,
            'percentage': 0,
            'message': 'Error checking engagement data'
        }, status=500)


@api_view(['GET'])
def list_alerts(request):
    """List all unresolved alerts"""
    try:
        alerts = Alert.objects.filter(is_resolved=False).order_by('-timestamp')[:20]
        return Response({'alerts': [{
            'id': a.id, 'type': a.alert_type, 'severity': a.severity,
            'message': a.message,
            'student_name': a.student.name if a.student else 'Class',
            'student_id': a.student.student_id if a.student else None,
            'time': a.timestamp.isoformat(),
        } for a in alerts], 'count': alerts.count()})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
def resolve_alert(request, alert_id):
    try:
        alert = Alert.objects.get(id=alert_id)
        alert.is_resolved = True
        alert.resolved_at = timezone.now()
        alert.save()
        return Response({'success': True})
    except Alert.DoesNotExist:
        return Response({'error': 'Alert not found'}, status=404)
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Attendance ───────────────────────────────────────────────────────────────

@api_view(['GET'])
def attendance_report(request):
    try:
        date_str = request.query_params.get('date')
        from datetime import date
        target_date = date.fromisoformat(date_str) if date_str else timezone.now().date()
        attendances = Attendance.objects.filter(date=target_date).select_related('student')

        # Auto-create notification for students whose rolling attendance is below 50%.
        for student in Student.objects.filter(is_active=True):
            student_att = Attendance.objects.filter(student=student)
            total = student_att.count()
            if total == 0:
                continue
            present = student_att.filter(is_present=True).count()
            rate = (present / total) * 100
            if rate < 50:
                _create_notification_if_needed(
                    'alert',
                    f'Attendance below 50% for {student.name} ({rate:.1f}%).',
                    related_student=student,
                    dedupe_hours=24,
                )

        return Response({
            'date': target_date.isoformat(),
            'attendance': [{
                'student_id': a.student.student_id, 'student_name': a.student.name,
                'is_present': a.is_present,
                'arrival_time': a.arrival_time.isoformat() if a.arrival_time else None,
                'seat_row': a.student.seat_row, 'seat_col': a.student.seat_col,
            } for a in attendances],
            'present_count': attendances.filter(is_present=True).count(),
            'absent_count': attendances.filter(is_present=False).count(),
            'total': attendances.count(),
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Analytics ────────────────────────────────────────────────────────────────

@api_view(['GET'])
def analytics_summary(request):
    try:
        days = int(request.query_params.get('days', 7))
        since = timezone.now() - timedelta(days=days)
        records = EngagementRecord.objects.filter(timestamp__gte=since)
        total_students = Student.objects.filter(is_active=True).count()

        attendance_qs = Attendance.objects.filter(date__gte=since.date())
        attendance_total = attendance_qs.count()
        attendance_present = attendance_qs.filter(is_present=True).count()
        attendance_rate = (attendance_present / attendance_total * 100) if attendance_total > 0 else 0

        if not records.exists():
            return Response({
                'message': 'No data available yet. Start a monitoring session.',
                'days': days, 'daily_engagement': [], 'hourly_pattern': [],
                'emotion_trend': {}, 'top_students': [], 'needs_attention': [],
                'overall_avg_engagement': 0,
                'total_students': total_students,
                'attendance_rate': round(attendance_rate, 1),
                'at_risk_count': 0,
                'performance_distribution': {
                    'excellent': 0,
                    'good': 0,
                    'average': 0,
                    'below_average': 0,
                },
            })

        df = pd.DataFrame(list(records.values(
            'timestamp', 'engagement_score', 'attention_score',
            'emotion', 'posture_score', 'eye_contact',
            'student__id', 'student__name', 'student__student_id'
        )))
        df.columns = ['timestamp', 'engagement_score', 'attention_score',
                      'emotion', 'posture_score', 'eye_contact',
                      'student_pk', 'student_name', 'student_id']
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour

        # Trend based on first vs latest record in selected period (not daily buckets)
        # so short sessions within one day still produce a meaningful trend.
        df_sorted = df.sort_values('timestamp')
        first_point = float(df_sorted['engagement_score'].iloc[0]) if len(df_sorted) > 0 else 0.0
        latest_point = float(df_sorted['engagement_score'].iloc[-1]) if len(df_sorted) > 0 else 0.0
        engagement_trend_diff = latest_point - first_point  # Keep for overall trend calculation

        daily_avg = df.groupby('date').agg({'engagement_score': 'mean', 'attention_score': 'mean'}).reset_index()
        daily_engagement = [{'date': str(row['date']), 'engagement': round(row['engagement_score'], 1),
                              'attention': round(row['attention_score'], 1)} for _, row in daily_avg.iterrows()]

        # Create proper engagement trend data for charts (date -> engagement percentage mapping)
        engagement_trend_chart = {str(row['date']): round(row['engagement_score'], 1) for _, row in daily_avg.iterrows()}

        hourly_avg = df.groupby('hour')['engagement_score'].mean().reset_index()
        hourly_pattern = [{'hour': f"{int(row['hour']):02d}:00", 'engagement': round(row['engagement_score'], 1)}
                          for _, row in hourly_avg.iterrows()]

        emotion_counts = df['emotion'].value_counts().to_dict()

        student_agg = (
            df.groupby(['student_pk', 'student_id', 'student_name'])
            .agg(
                engagement_score=('engagement_score', 'mean'),
                focus_score=('attention_score', 'mean'),
                participation_score=('eye_contact', 'mean'),
            )
            .reset_index()
        )

        # Attendance rate by student in selected period.
        attendance_rates = {
            row['student_id']: round(
                (float(row['present_days']) / float(row['total_days']) * 100.0) if row['total_days'] else 0.0,
                1,
            )
            for row in (
                Attendance.objects
                .filter(date__gte=since.date(), student__is_active=True)
                .values('student__student_id')
                .annotate(
                    student_id=Max('student__student_id'),
                    total_days=Count('id'),
                    present_days=Count('id', filter=Q(is_present=True)),
                )
            )
        }

        student_agg['attendance_score'] = student_agg['student_id'].map(lambda sid: attendance_rates.get(sid, 0.0))
        student_agg['participation_score'] = student_agg['participation_score'].fillna(0.0) * 100.0

        # Weighted score for ranking.
        student_agg['ranking_score'] = (
            (student_agg['engagement_score'] * 0.50)
            + (student_agg['attendance_score'] * 0.20)
            + (student_agg['focus_score'] * 0.20)
            + (student_agg['participation_score'] * 0.10)
        )
        student_avg_sorted = student_agg.sort_values('ranking_score', ascending=False)

        top_students = [{
            'student_id': row['student_id'],
            'name': row['student_name'],
            'avg_engagement': round(float(row['engagement_score']), 1),
            'attendance': round(float(row['attendance_score']), 1),
            'focus': round(float(row['focus_score']), 1),
            'participation': round(float(row['participation_score']), 1),
            'ranking_score': round(float(row['ranking_score']), 1),
        } for _, row in student_avg_sorted.head(5).iterrows()]
        at_risk_df = student_avg_sorted[student_avg_sorted['engagement_score'] < 60]
        needs_attention = [{'student_id': row['student_id'], 'name': row['student_name'],
                            'avg_engagement': round(row['engagement_score'], 1)}
                           for _, row in at_risk_df.head(5).iterrows()]

        excellent_count = int((student_agg['engagement_score'] >= 90).sum())
        good_count = int(((student_agg['engagement_score'] >= 80) & (student_agg['engagement_score'] < 90)).sum())
        average_count = int(((student_agg['engagement_score'] >= 70) & (student_agg['engagement_score'] < 80)).sum())
        below_average_count = int((student_agg['engagement_score'] < 70).sum())

        return Response({
            'days': days, 'total_records': len(df),
            'daily_engagement': daily_engagement, 'hourly_pattern': hourly_pattern,
            'emotion_trend': emotion_counts, 'top_students': top_students,
            'student_rankings': top_students,
            'needs_attention': needs_attention,
            'overall_avg_engagement': round(df['engagement_score'].mean(), 1),
            'engagement_trend': round(float(engagement_trend_diff), 1),
            'trend_has_data': bool(len(df_sorted) > 1),
            'total_students': total_students,
            'attendance_rate': round(attendance_rate, 1),
            'at_risk_count': int(len(at_risk_df)),
            'performance_distribution': {
                'excellent': excellent_count,
                'good': good_count,
                'average': average_count,
                'below_average': below_average_count,
            },
        })
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        return Response({'error': str(e)}, status=500)


# ─── Heatmap ──────────────────────────────────────────────────────────────────

@api_view(['GET'])
def classroom_heatmap(request):
    try:
        today = timezone.now().date()
        students = Student.objects.filter(is_active=True)
        heatmap_data = []
        active_session = ClassSession.objects.filter(status='active').order_by('-start_time').first()
        aggregation = 'session_avg' if active_session else 'today_avg'

        # Keep presence definition consistent with dashboard/list_students:
        # present in any attendance row for today.
        present_student_ids = set(
            Attendance.objects.filter(
                date=today,
                is_present=True,
            ).values_list('student_id', flat=True)
        )

        live_by_student_id = {}
        try:
            from .video_stream import get_video_stream
            live_analysis = get_video_stream().get_latest_analysis() or {}
            for item in (live_analysis.get('recognized_students') or []):
                sid = item.get('student_id')
                if sid:
                    live_by_student_id[sid] = item
        except Exception:
            live_by_student_id = {}

        for student in students:
            if active_session:
                records_qs = EngagementRecord.objects.filter(student=student, session=active_session)
            else:
                records_qs = EngagementRecord.objects.filter(student=student, timestamp__date=today)

            metrics = records_qs.aggregate(avg_engagement=Avg('engagement_score'))
            dominant_emotion_row = (
                records_qs.values('emotion')
                .annotate(total=Count('id'))
                .order_by('-total', 'emotion')
                .first()
            )
            live_item = live_by_student_id.get(student.student_id)

            engagement = metrics.get('avg_engagement')
            emotion = dominant_emotion_row.get('emotion') if dominant_emotion_row else None
            present = student.id in present_student_ids

            if live_item is not None:
                if engagement is None:
                    engagement = float(live_item.get('engagement') or 0)
                if not emotion:
                    emotion = str(live_item.get('emotion') or 'unknown')
                present = True

            if engagement is None:
                engagement = 0
            if not emotion:
                emotion = 'unknown'

            if not present:
                level, color = 'absent', '#374151'
            elif engagement >= 80:
                level, color = 'high', '#22c55e'
            elif engagement >= 60:
                level, color = 'medium', '#3b82f6'
            elif engagement >= 40:
                level, color = 'low', '#f59e0b'
            else:
                level, color = 'very_low', '#ef4444'

            heatmap_data.append({
                'student_id': student.student_id, 'name': student.name,
                'seat_row': student.seat_row, 'seat_col': student.seat_col,
                'engagement': round(engagement, 1), 'emotion': emotion,
                'present': present, 'level': level, 'color': color,
            })
        return Response({
            'heatmap': heatmap_data,
            'aggregation': aggregation,
            'session_id': active_session.id if active_session else None,
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Session Report ───────────────────────────────────────────────────────────

@api_view(['GET'])
def session_report(request, session_id):
    try:
        session = ClassSession.objects.get(id=session_id)
        records = EngagementRecord.objects.filter(session=session)
        snapshots = ClassEngagementSnapshot.objects.filter(session=session).order_by('timestamp')
        attendance = Attendance.objects.filter(session=session)
        alerts_list = Alert.objects.filter(session=session)

        if records.exists():
            df = pd.DataFrame(list(records.values('timestamp', 'engagement_score', 'emotion', 'student__name')))
            df.columns = ['timestamp', 'engagement_score', 'emotion', 'student_name']
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            overall_avg = df['engagement_score'].mean()
            emotion_dist = df['emotion'].value_counts().to_dict()
            timeline = [{'time': timezone.localtime(s.timestamp).strftime('%H:%M:%S'), 'engagement': round(s.avg_engagement, 1),
                         'present': s.present_count} for s in snapshots]
        else:
            overall_avg = 0
            emotion_dist = {}
            timeline = []

        return Response({
            'session': {
                'id': session.id, 'class_name': session.class_name, 'subject': session.subject,
                'start_time': session.start_time.isoformat(),
                'end_time': session.end_time.isoformat() if session.end_time else None,
                'duration_minutes': session.duration_minutes, 'status': session.status,
            },
            'summary': {
                'total_students': attendance.count(), 'present': attendance.filter(is_present=True).count(),
                'avg_engagement': round(overall_avg, 1), 'total_alerts': alerts_list.count(),
            },
            'emotion_distribution': emotion_dist, 'engagement_timeline': timeline,
        })
    except ClassSession.DoesNotExist:
        return Response({'error': 'Session not found'}, status=404)
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Seed Data ────────────────────────────────────────────────────────────────

@api_view(['POST'])
def seed_demo_data(request):
    try:
        import random
        student_names = [
            "Emma Johnson", "Liam Smith", "Olivia Davis", "Noah Wilson",
            "Ava Martinez", "Ethan Brown", "Sophia Garcia", "Mason Taylor",
            "Isabella Anderson", "William Thomas", "Mia Jackson", "James White",
            "Charlotte Harris", "Benjamin Lewis", "Amelia Walker", "Lucas Hall",
            "Harper Young", "Henry King", "Evelyn Wright", "Alexander Scott",
            "Abigail Green", "Michael Adams", "Emily Baker", "Daniel Carter"
        ]
        created = 0
        for i, name in enumerate(student_names):
            sid = f"STU{i+1:03d}"
            if not Student.objects.filter(student_id=sid).exists():
                Student.objects.create(
                    student_id=sid, name=name,
                    email=f"{name.lower().replace(' ', '.')}@student.edu",
                    seat_row=(i // 8) + 1, seat_col=(i % 8) + 1,
                )
                created += 1

        # Ensure we have a teacher + an ended "seed" session to attach records to
        teacher, _ = Teacher.objects.get_or_create(
            email='demo@smartclass.com',
            defaults={'name': 'Demo Teacher', 'password_hash': hash_password('demo123'), 'subject': 'Computer Science'}
        )
        seed_session, _ = ClassSession.objects.get_or_create(
            teacher=teacher,
            class_name='CS101',
            subject='Computer Science',
            status='ended',
            defaults={
                'start_time': timezone.now() - timedelta(days=30),
                'end_time': timezone.now() - timedelta(days=30) + timedelta(hours=1),
                'camera_source': '0',
                'total_students': Student.objects.filter(is_active=True).count(),
            }
        )

        students = list(Student.objects.filter(is_active=True))
        if students:
            # ── Attendance: last 28 days (unique_together prevents duplicates) ──
            attendance_created = 0
            for days_ago in range(27, -1, -1):
                d = timezone.now().date() - timedelta(days=days_ago)
                # weekdays more present than weekends
                weekday = d.weekday()  # Mon=0..Sun=6
                base_present_rate = 0.92 if weekday < 5 else 0.75

                for s in students:
                    is_present = random.random() < base_present_rate
                    arrival_time = None
                    if is_present:
                        # Arrive between 08:55 and 09:10
                        minutes = random.randint(0, 15)
                        arrival_time = timezone.make_aware(datetime.combine(d, datetime.min.time())) + timedelta(
                            hours=9, minutes=minutes
                        )
                    obj, was_created = Attendance.objects.get_or_create(
                        student=s,
                        session=seed_session,
                        date=d,
                        defaults={
                            'is_present': is_present,
                            'arrival_time': arrival_time,
                            'detection_confidence': round(random.uniform(0.75, 0.98), 2) if is_present else 0.0,
                        }
                    )
                    if was_created:
                        attendance_created += 1

            # ── Engagement records: last 7 days, a few samples per day/student ──
            emotions = ['happy', 'neutral', 'confused', 'bored', 'focused']
            engagement_created = 0
            snapshot_created = 0

            now = timezone.now()
            for days_ago in range(6, -1, -1):
                d = now.date() - timedelta(days=days_ago)
                # 4 samples across the class time window
                sample_times = [9, 10, 11, 12]  # hours

                # generate student-level records
                per_sample_avgs = []
                for hour in sample_times:
                    ts = timezone.make_aware(datetime.combine(d, datetime.min.time())) + timedelta(
                        hours=hour, minutes=random.randint(0, 45)
                    )
                    # keep timestamps not in the future
                    if ts > now:
                        continue

                    present_ids = set(
                        Attendance.objects.filter(session=seed_session, date=d, is_present=True)
                        .values_list('student_id', flat=True)
                    )

                    eng_vals = []
                    att_vals = []
                    emo_dist = {e: 0 for e in emotions}

                    for s in students:
                        # If absent that day, still create a low record sometimes (rare) to keep DB realistic
                        if s.id not in present_ids and random.random() > 0.08:
                            continue

                        # Create a "student baseline" so top performers / at-risk exist
                        sid_num = int(s.student_id.replace('STU', '') or 0)
                        baseline = 72 + (sid_num % 7) * 2  # mild spread
                        noise = random.uniform(-18, 18)
                        engagement = max(5, min(98, baseline + noise))
                        attention = max(5, min(98, engagement + random.uniform(-10, 10)))

                        # Map low engagement to bored/confused more often
                        if engagement < 55:
                            emo = random.choices(['bored', 'confused', 'neutral'], weights=[0.45, 0.35, 0.20])[0]
                        elif engagement > 85:
                            emo = random.choices(['happy', 'focused', 'neutral'], weights=[0.35, 0.45, 0.20])[0]
                        else:
                            emo = random.choices(['neutral', 'focused', 'happy', 'confused'], weights=[0.45, 0.25, 0.15, 0.15])[0]

                        emo_dist[emo] = emo_dist.get(emo, 0) + 1
                        eng_vals.append(engagement)
                        att_vals.append(attention)

                        _, was_created = EngagementRecord.objects.get_or_create(
                            student=s,
                            session=seed_session,
                            timestamp=ts,
                            defaults={
                                'engagement_score': round(engagement, 1),
                                'attention_score': round(attention, 1),
                                'emotion': emo,
                                'emotion_confidence': round(random.uniform(0.6, 0.95), 2),
                                'posture_score': round(max(10, min(95, attention + random.uniform(-15, 15))), 1),
                                'eye_contact': attention > 70,
                                'face_detected': True,
                                'face_confidence': round(random.uniform(0.7, 0.98), 2),
                            }
                        )
                        if was_created:
                            engagement_created += 1

                    # class snapshot for that sample time
                    if eng_vals:
                        avg_eng = sum(eng_vals) / len(eng_vals)
                        avg_att = sum(att_vals) / len(att_vals)
                        present_count = len(eng_vals)
                        confusion_alert = (emo_dist.get('confused', 0) / max(present_count, 1)) >= 0.30
                        low_eng_alert = avg_eng < 60
                        _, was_created = ClassEngagementSnapshot.objects.get_or_create(
                            session=seed_session,
                            timestamp=ts,
                            defaults={
                                'avg_engagement': round(avg_eng, 1),
                                'avg_attention': round(avg_att, 1),
                                'present_count': present_count,
                                'emotion_distribution': json.dumps(emo_dist),
                                'confusion_alert': confusion_alert,
                                'low_engagement_alert': low_eng_alert,
                            }
                        )
                        if was_created:
                            snapshot_created += 1

            return Response({
                'success': True,
                'students_created': created,
                'total_students': Student.objects.count(),
                'attendance_created': attendance_created,
                'engagement_created': engagement_created,
                'snapshot_created': snapshot_created
            })
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
def create_test_engagement_data(request):
    """Create test engagement data to trigger alerts"""
    try:
        from django.utils import timezone
        from datetime import timedelta
        import random
        
        # Get or create active session
        active_session = ClassSession.objects.filter(status='active').first()
        if not active_session:
            # Create a test session
            teacher = Teacher.objects.first()
            if not teacher:
                return Response({'error': 'No teacher found'}, status=400)
            
            active_session = ClassSession.objects.create(
                teacher=teacher,
                class_name='CS101',
                subject='Computer Science',
                status='active'
            )
        
        # Get students
        students = Student.objects.all()[:5]  # Test with 5 students
        
        # Create engagement records with high confusion
        for student in students:
            # Create 3 records per student with confused emotion
            for i in range(3):
                EngagementRecord.objects.create(
                    student=student,
                    session=active_session,
                    timestamp=timezone.now() - timedelta(minutes=i),
                    engagement_score=random.uniform(20, 35),  # Low engagement
                    attention_score=random.uniform(15, 30),
                    emotion='confused',  # Confused emotion
                    emotion_confidence=random.uniform(0.7, 0.9),
                    face_detected=True,
                    face_confidence=random.uniform(0.8, 0.95)
                )
        
        return Response({
            'success': True,
            'message': f'Created test engagement data for {students.count()} students'
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def api_health(request):
    from .video_stream import get_video_stream
    stream = get_video_stream()
    return Response({
        'status': 'ok', 'timestamp': timezone.now().isoformat(),
        'database': 'connected', 'stream_running': stream.is_running,
        'students': Student.objects.count(),
        'sessions': ClassSession.objects.filter(status='active').count(),
    })


@api_view(['GET'])
def model_status(request):
    """Get status of all AI models (FER-2013, DAiSEE, etc.)"""
    from .camera import camera_processor
    import os
    
    fer2013_loaded = camera_processor.fer2013_predictor is not None
    daisee_loaded = camera_processor.daisee_predictor is not None
    fer_loaded = camera_processor._fer_detector is not None
    
    return Response({
        'timestamp': timezone.now().isoformat(),
        'models': {
            'fer': {
                'loaded': fer_loaded,
                'status': 'ready' if fer_loaded else 'not_initialized',
                'description': 'Core emotion detector (FER package)',
            },
            'fer2013': {
                'loaded': fer2013_loaded,
                'status': 'ready' if fer2013_loaded else 'not_loaded',
                'checkpoint': camera_processor.fer2013_model_path,
                'exists': os.path.exists(camera_processor.fer2013_model_path),
                'description': 'Fallback emotion detector trained on FER-2013 dataset',
            },
            'daisee': {
                'loaded': daisee_loaded,
                'status': 'ready' if daisee_loaded else 'not_loaded',
                'checkpoint': camera_processor.daisee_model_path,
                'exists': os.path.exists(camera_processor.daisee_model_path),
                'description': 'Engagement level detector (DAiSEE-inspired)',
            },
        },
        'fusion': {
            'enabled': camera_processor.fusion_enabled,
            'fer_weight': round(camera_processor.fer_weight, 3),
            'daisee_weight': round(camera_processor.daisee_weight, 3),
        },
        'camera': {
            'running': camera_processor.is_running,
            'demo_mode': camera_processor.demo_mode,
        },
    })


# ─── Reports Endpoints ───────────────────────────────────────────────────────

@api_view(['GET'])
def list_reports(request):
    """List all generated reports"""
    try:
        # FIXED: Query actual Report objects from DB
        reports = Report.objects.all().order_by('-created_at')
        
        report_data = []
        for report in reports:
            report_data.append({
                'id': report.id,
                'name': report.name,
                'type': report.get_report_type_display(),
                'report_type': report.report_type,
                'date': report.created_at.strftime('%Y-%m-%d'),
                'format': report.format.upper(),
                'size': f"{report.file_size / (1024*1024):.1f} MB" if report.file_size else "N/A",
                'status': report.status,
                'created_at': report.created_at.isoformat(),
                'generated_at': report.generated_at.isoformat() if report.generated_at else None
            })
        
        return Response({
            'success': True,
            'reports': report_data
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def generate_report(request):
    """Generate a new report"""
    try:
        import pandas as pd
        import os
        from django.conf import settings
        
        data = request.data
        report_type = data.get('type', 'engagement')
        date_range = data.get('date_range', '7')
        class_name = data.get('class', 'CS101')
        format_type = str(data.get('format', 'csv')).strip().lower()
        supported_formats = {'csv', 'xlsx', 'pdf'}
        if format_type not in supported_formats:
            return Response({
                'error': f"Unsupported format '{format_type}'. Supported formats: CSV, Excel (XLSX), PDF."
            }, status=400)
        
        # FIXED: Actually generate a CSV report using pandas from real data
        # Create report record
        report = Report.objects.create(
            name=f'{report_type.title()} Report - {class_name}',
            report_type=report_type,
            format=format_type,
            status='generating'
        )
        
        # Get date range
        days = int(date_range) if date_range.isdigit() else 7
        date_from = timezone.now().date() - timedelta(days=days)
        date_to = timezone.now().date()
        
        report.date_from = date_from
        report.date_to = date_to
        report.save()
        
        # Generate report data based on type
        if report_type == 'engagement':
            # Get engagement records
            records = EngagementRecord.objects.filter(
                timestamp__date__gte=date_from,
                timestamp__date__lte=date_to
            ).select_related('student')
            
            # Create DataFrame
            data = []
            for record in records:
                data.append({
                    'Student Name': record.student.name,
                    'Student ID': record.student.student_id,
                    'Date': record.timestamp.date(),
                    'Time': record.timestamp.time(),
                    'Engagement Score': record.engagement_score,
                    'Attention Score': record.attention_score,
                    'Emotion': record.emotion,
                    'Posture Score': record.posture_score
                })
            
            df = pd.DataFrame(data)
            
        elif report_type == 'attendance':
            # Get attendance records
            records = Attendance.objects.filter(
                date__gte=date_from,
                date__lte=date_to
            ).select_related('student', 'session')
            
            data = []
            for record in records:
                data.append({
                    'Student Name': record.student.name,
                    'Student ID': record.student.student_id,
                    'Date': record.date,
                    'Status': 'Present' if record.is_present else 'Absent',
                    'Arrival Time': record.arrival_time,
                    'Session': record.session.class_name if record.session else 'N/A',
                    'Detection Confidence': record.detection_confidence
                })
            
            df = pd.DataFrame(data)
            
        else:  # summary report
            # Combine engagement and attendance
            engagement_records = EngagementRecord.objects.filter(
                timestamp__date__gte=date_from,
                timestamp__date__lte=date_to
            ).select_related('student')
            
            data = []
            for record in engagement_records:
                data.append({
                    'Student Name': record.student.name,
                    'Student ID': record.student.student_id,
                    'Date': record.timestamp.date(),
                    'Engagement Score': record.engagement_score,
                    'Attention Score': record.attention_score,
                    'Emotion': record.emotion
                })
            
            df = pd.DataFrame(data)
        
        # Create reports directory if it doesn't exist
        reports_dir = os.path.join(settings.MEDIA_ROOT, 'reports')
        os.makedirs(reports_dir, exist_ok=True)
        
        # Generate and save file. If XLSX writer is unavailable in the active
        # server environment, gracefully fall back to CSV instead of failing.
        base_filename = f"{report.name.replace(' ', '_').lower()}_{report.id}"
        actual_format = format_type

        if format_type == 'csv':
            filename = f"{base_filename}.csv"
            file_path = os.path.join(reports_dir, filename)
            df.to_csv(file_path, index=False)
        elif format_type == 'xlsx':
            filename = f"{base_filename}.xlsx"
            file_path = os.path.join(reports_dir, filename)
            try:
                df.to_excel(file_path, index=False)
            except Exception as excel_error:
                logger.warning(f"Excel generation failed, falling back to CSV: {excel_error}")
                actual_format = 'csv'
                filename = f"{base_filename}.csv"
                file_path = os.path.join(reports_dir, filename)
                df.to_csv(file_path, index=False)
        elif format_type == 'pdf':
            filename = f"{base_filename}.pdf"
            file_path = os.path.join(reports_dir, filename)
            try:
                from reportlab.lib import colors  # type: ignore[import-not-found]
                from reportlab.lib.pagesizes import A4, landscape  # type: ignore[import-not-found]
                from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import-not-found]
                from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle  # type: ignore[import-not-found]

                doc = SimpleDocTemplate(file_path, pagesize=landscape(A4))
                styles = getSampleStyleSheet()
                story = []

                story.append(Paragraph(f"PCU Hackthon - {report.name}", styles['Title']))
                story.append(Paragraph(f"Generated: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
                story.append(Spacer(1, 12))

                display_df = df.fillna('')
                header = list(display_df.columns)
                rows = display_df.astype(str).values.tolist()
                table_data = [header] + rows

                if len(table_data) > 401:
                    table_data = table_data[:401]

                table = Table(table_data, repeatRows=1)
                table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4f8ef7')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 8),
                    ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
                ]))
                story.append(table)
                doc.build(story)
            except Exception as pdf_error:
                logger.warning(f"PDF generation failed, falling back to CSV: {pdf_error}")
                actual_format = 'csv'
                filename = f"{base_filename}.csv"
                file_path = os.path.join(reports_dir, filename)
                df.to_csv(file_path, index=False)
        
        # Update report record
        report.format = actual_format
        report.file_path = f"reports/{filename}"
        report.status = 'completed'
        report.generated_at = timezone.now()
        report.file_size = os.path.getsize(file_path)
        report.save()
        
        return Response({
            'success': True,
            'message': f'Report generated successfully: {report.name}',
            'report': {
                'id': report.id,
                'name': report.name,
                'type': report.get_report_type_display(),
                'date': report.created_at.strftime('%Y-%m-%d'),
                'format': report.format.upper(),
                'size': f"{report.file_size / (1024*1024):.1f} MB",
                'status': report.status
            }
        })
        
    except Exception as e:
        # Update report status to failed
        if 'report' in locals():
            report.status = 'failed'
            report.save()
        return Response({'error': str(e)}, status=500)

@api_view(['GET'])
def download_report(request, report_id):
    """Download a specific report"""
    try:
        from django.http import FileResponse
        from django.conf import settings
        import os
        
        # FIXED: Serve the actual saved file using Django FileResponse
        try:
            report = Report.objects.get(id=report_id)
        except Report.DoesNotExist:
            return Response({'error': 'Report not found'}, status=404)
        
        if not report.file_path or report.status != 'completed':
            return Response({'error': 'Report file not available'}, status=404)
        
        # Build full file path
        file_path = os.path.join(settings.MEDIA_ROOT, report.file_path)
        
        if not os.path.exists(file_path):
            return Response({'error': 'Report file not found on server'}, status=404)
        
        # Return file response
        response = FileResponse(
            open(file_path, 'rb'),
            as_attachment=True,
            filename=f"{report.name}.{report.format}"
        )
        
        return response
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@csrf_exempt
@api_view(['DELETE', 'POST'])
@authentication_classes([])
@permission_classes([])
def delete_report(request, report_id):
    """Delete a specific report"""
    try:
        import os
        from django.conf import settings
        
        # Support both numeric and string payload IDs robustly.
        try:
            report_pk = int(str(report_id).strip())
        except Exception:
            return Response({'error': 'Invalid report id'}, status=400)

        # FIXED: Actually delete the report from database and file system
        try:
            report = Report.objects.get(id=report_pk)
        except Report.DoesNotExist:
            return Response({'error': 'Report not found'}, status=404)
        
        # Delete file if it exists
        if report.file_path:
            file_path = os.path.join(settings.MEDIA_ROOT, report.file_path)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as file_error:
                    logger.warning(f"Report file delete warning (continuing): {file_error}")
        
        # Delete database record
        report.delete()
        
        return Response({
            'success': True,
            'message': f'Report {report_pk} deleted successfully'
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['GET'])
def report_templates(request):
    """Get available report templates"""
    try:
        templates = [
            {
                'id': 'tpl_001',
                'name': 'Daily Performance Template',
                'description': 'Comprehensive daily classroom performance analysis',
                'sections': ['attendance', 'engagement', 'emotions', 'participation']
            },
            {
                'id': 'tpl_002',
                'name': 'Weekly Summary Template',
                'description': 'Weekly overview of classroom metrics and trends',
                'sections': ['summary', 'trends', 'top_performers', 'alerts']
            },
            {
                'id': 'tpl_003',
                'name': 'Individual Student Report',
                'description': 'Detailed analysis for individual student performance',
                'sections': ['engagement', 'attendance', 'emotions', 'recommendations']
            }
        ]
        
        return Response({
            'success': True,
            'templates': templates
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['POST'])
def schedule_report(request):
    """Schedule a report to be generated automatically"""
    try:
        data = request.data
        schedule_time = data.get('schedule_time', '')
        report_type = data.get('type', 'Daily')
        
        # Mock scheduling - in production this would create a scheduled task
        return Response({
            'success': True,
            'message': f'Report scheduled for {schedule_time}',
            'scheduled_time': schedule_time
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Teacher Dashboard APIs ─────────────────────────────────────────────────

def _get_default_teacher():
    teacher = Teacher.objects.filter(is_active=True).first()
    if teacher:
        return teacher
    return Teacher.objects.create(
        name='Demo Teacher',
        email='demo.teacher@smartclass.com',
        password_hash=hash_password('demo123'),
        subject='General',
        is_active=True,
    )


def _get_request_teacher(request):
    teacher_id = request.session.get('teacher_id')
    if teacher_id:
        teacher = Teacher.objects.filter(id=teacher_id, is_active=True).first()
        if teacher:
            return teacher
    return _get_default_teacher()


def _serialize_topic(topic):
    return {
        'id': topic.id,
        'subject': topic.subject,
        'unit': topic.unit,
        'topic': topic.topic,
        'status': topic.status,
        'delayed': bool(topic.is_delayed),
        'plannedDate': topic.planned_date.isoformat() if topic.planned_date else None,
        'revisedDate': topic.revised_date.isoformat() if topic.revised_date else None,
        'checkpointAssigned': bool(topic.checkpoint_assigned),
        'checkpointRate': round(float(topic.checkpoint_completion_rate or 0.0), 1),
    }


def _ensure_teacher_seed_data(teacher):
    if SyllabusTopic.objects.filter(teacher=teacher).exists():
        return

    today = timezone.now().date()
    seed_topics = [
        ('Computer Science', 'Unit 1', 'Introduction to Algorithms', 'completed', 85.0),
        ('Computer Science', 'Unit 1', 'Sorting Algorithms', 'in-progress', 65.0),
        ('Computer Science', 'Unit 2', 'Data Structures', 'pending', 0.0),
    ]

    created = []
    for idx, (subject, unit, title, state, rate) in enumerate(seed_topics):
        created.append(SyllabusTopic.objects.create(
            teacher=teacher,
            subject=subject,
            unit=unit,
            topic=title,
            status=state,
            planned_date=today - timedelta(days=(3 - idx)),
            checkpoint_assigned=(state != 'pending'),
            checkpoint_completion_rate=rate,
        ))

    if len(created) > 1:
        DailyLectureTopic.objects.get_or_create(topic=created[1], lecture_date=today)

    for topic in created:
        for student in Student.objects.filter(is_active=True):
            base = 45 + ((student.id * 13 + topic.id * 11) % 46)
            StudentTopicProgress.objects.get_or_create(
                student=student,
                topic=topic,
                defaults={
                    'completion_percent': float(base),
                    'needs_extra_lecture': base < 60,
                }
            )


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def teacher_dashboard_data(request):
    try:
        teacher = _get_request_teacher(request)
        _ensure_teacher_seed_data(teacher)

        topics = list(SyllabusTopic.objects.filter(teacher=teacher).order_by('created_at'))
        today = timezone.now().date()

        planner_qs = (
            DailyLectureTopic.objects
            .filter(topic__teacher=teacher, lecture_date=today)
            .select_related('topic')
            .order_by('topic__created_at')
        )
        planner = [{
            'id': p.id,
            'topicId': p.topic_id,
            'subject': p.topic.subject,
            'topic': p.topic.topic,
            'unit': p.topic.unit,
            'done': bool(p.is_completed),
        } for p in planner_qs]

        progress_rows = {
            row['topic_id']: row
            for row in (
                StudentTopicProgress.objects
                .filter(topic__teacher=teacher)
                .values('topic_id')
                .annotate(
                    total_students=Count('id'),
                    responded_students=Count('id', filter=Q(completion_percent__gte=60)),
                )
            )
        }

        checkpoints = []
        for idx, topic in enumerate(topics, start=1):
            stats = progress_rows.get(topic.id, {})
            checkpoints.append({
                'id': topic.id,
                'topicNumber': idx,
                'topic': topic.topic,
                'checkpointAssigned': bool(topic.checkpoint_assigned),
                'checkpointRate': round(float(topic.checkpoint_completion_rate or 0.0), 1),
                'respondedStudents': int(stats.get('responded_students') or 0),
                'totalStudents': int(stats.get('total_students') or 0),
            })

        delayed = [{
            'id': t.id,
            'topic': t.topic,
            'plannedDate': t.planned_date.isoformat() if t.planned_date else None,
            'revisedDate': t.revised_date.isoformat() if t.revised_date else None,
        } for t in topics if t.is_delayed]

        lagging = [{
            'studentId': row.student.student_id,
            'name': row.student.name,
            'topicId': row.topic_id,
            'topic': row.topic.topic,
            'engagement': round(float(row.completion_percent or 0.0), 1),
        } for row in (
            StudentTopicProgress.objects
            .select_related('student', 'topic')
            .filter(topic__teacher=teacher, completion_percent__lt=60)
            .order_by('completion_percent')[:25]
        )]

        extra_by_student = {
            row['student__student_id']: int(row['count'])
            for row in (
                ExtraLecturePlan.objects
                .filter(topic__teacher=teacher)
                .values('student__student_id')
                .annotate(count=Count('id'))
            )
        }

        performance = []
        for student in Student.objects.filter(is_active=True).order_by('name'):
            att_qs = Attendance.objects.filter(student=student)
            att_total = att_qs.count()
            att_present = att_qs.filter(is_present=True).count()
            attendance_pct = (att_present / att_total * 100.0) if att_total > 0 else 0.0

            eng_avg = EngagementRecord.objects.filter(student=student).aggregate(v=Avg('engagement_score')).get('v') or 0.0
            completion_avg = (
                StudentTopicProgress.objects
                .filter(student=student, topic__teacher=teacher)
                .aggregate(v=Avg('completion_percent'))
                .get('v') or 0.0
            )

            performance.append({
                'studentId': student.student_id,
                'name': student.name,
                'engagement': round(float(eng_avg), 1),
                'attendance': round(float(attendance_pct), 1),
                'completion': round(float(completion_avg), 1),
                'extraLectures': int(extra_by_student.get(student.student_id, 0)),
            })

        feedback = [{
            'id': f.id,
            'lecture': f.lecture_title,
            'rating': round(float(f.rating or 0.0), 1),
            'comment': f.comment,
            'submittedAt': f.submitted_at.isoformat(),
        } for f in LectureFeedback.objects.order_by('-submitted_at')[:30]]

        avg_rating = LectureFeedback.objects.aggregate(v=Avg('rating')).get('v') or 0.0
        active_session = ClassSession.objects.filter(status='active').order_by('-start_time').first()

        return Response({
            'success': True,
            'session': {
                'id': active_session.id if active_session else None,
                'className': active_session.class_name if active_session else None,
                'subject': active_session.subject if active_session else None,
                'topicCount': len(topics),
                'topicNames': [t.topic for t in topics],
            },
            'topics': [_serialize_topic(t) for t in topics],
            'planner': planner,
            'checkpoints': checkpoints,
            'delayed': delayed,
            'lagging': lagging,
            'performance': performance,
            'feedback': feedback,
            'avgRating': round(float(avg_rating), 1),
        })
    except Exception as e:
        logger.error(f"Teacher dashboard data error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_add_syllabus_topic(request):
    try:
        teacher = _get_request_teacher(request)
        subject = str(request.data.get('subject') or '').strip()
        unit = str(request.data.get('unit') or '').strip()
        topic_text = str(request.data.get('topic') or '').strip()
        if not subject or not unit or not topic_text:
            return Response({'success': False, 'error': 'subject, unit and topic are required'}, status=400)

        topic = SyllabusTopic.objects.create(
            teacher=teacher,
            subject=subject,
            unit=unit,
            topic=topic_text,
            planned_date=timezone.now().date(),
        )

        for student in Student.objects.filter(is_active=True):
            StudentTopicProgress.objects.get_or_create(
                student=student,
                topic=topic,
                defaults={'completion_percent': 50.0, 'needs_extra_lecture': True},
            )

        return Response({'success': True, 'topic': _serialize_topic(topic)})
    except Exception as e:
        logger.error(f"Add syllabus topic error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_update_topic_status(request, topic_id):
    try:
        teacher = _get_request_teacher(request)
        topic = SyllabusTopic.objects.get(id=topic_id, teacher=teacher)
        new_status = str(request.data.get('status') or '').strip().lower()
        if new_status not in {'pending', 'in-progress', 'completed'}:
            return Response({'success': False, 'error': 'Invalid status'}, status=400)

        topic.status = new_status
        if new_status == 'completed':
            topic.checkpoint_assigned = True
            topic.checkpoint_completion_rate = max(float(topic.checkpoint_completion_rate or 0.0), 75.0)
            StudentTopicProgress.objects.filter(topic=topic).update(completion_percent=85.0, needs_extra_lecture=False)
        topic.save()
        return Response({'success': True, 'topic': _serialize_topic(topic)})
    except SyllabusTopic.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        logger.error(f"Update topic status error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_add_daily_topic(request):
    try:
        teacher = _get_request_teacher(request)
        topic_id = request.data.get('topic_id')
        if not topic_id:
            return Response({'success': False, 'error': 'topic_id is required'}, status=400)

        topic = SyllabusTopic.objects.get(id=topic_id, teacher=teacher)
        plan, _ = DailyLectureTopic.objects.get_or_create(topic=topic, lecture_date=timezone.now().date())
        return Response({'success': True, 'plan': {'id': plan.id, 'topicId': topic.id, 'topic': topic.topic, 'unit': topic.unit, 'done': bool(plan.is_completed)}})
    except SyllabusTopic.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        logger.error(f"Add daily topic error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_complete_daily_topic(request, plan_id):
    try:
        teacher = _get_request_teacher(request)
        plan = DailyLectureTopic.objects.select_related('topic').get(id=plan_id, topic__teacher=teacher)
        plan.is_completed = True
        plan.completed_at = timezone.now()
        plan.save(update_fields=['is_completed', 'completed_at'])

        topic = plan.topic
        topic.status = 'completed'
        topic.checkpoint_assigned = True
        topic.checkpoint_completion_rate = max(float(topic.checkpoint_completion_rate or 0.0), 75.0)
        topic.save(update_fields=['status', 'checkpoint_assigned', 'checkpoint_completion_rate'])
        StudentTopicProgress.objects.filter(topic=topic).update(completion_percent=85.0, needs_extra_lecture=False)

        return Response({'success': True, 'nextTopicLaunched': None})
    except DailyLectureTopic.DoesNotExist:
        return Response({'success': False, 'error': 'Planned topic not found'}, status=404)
    except Exception as e:
        logger.error(f"Complete daily topic error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_schedule_extra_lecture(request):
    try:
        teacher = _get_request_teacher(request)
        student_id = str(request.data.get('student_id') or '').strip()
        topic_id = request.data.get('topic_id')
        if not student_id or not topic_id:
            return Response({'success': False, 'error': 'student_id and topic_id are required'}, status=400)

        student = Student.objects.get(student_id=student_id)
        topic = SyllabusTopic.objects.get(id=topic_id, teacher=teacher)
        plan = ExtraLecturePlan.objects.create(
            student=student,
            topic=topic,
            scheduled_date=timezone.now().date() + timedelta(days=2),
        )
        StudentTopicProgress.objects.filter(student=student, topic=topic).update(needs_extra_lecture=True)
        return Response({'success': True, 'message': f'Extra lecture scheduled for {student.name}', 'planId': plan.id})
    except Student.DoesNotExist:
        return Response({'success': False, 'error': 'Student not found'}, status=404)
    except SyllabusTopic.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        logger.error(f"Schedule extra lecture error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_add_feedback(request):
    try:
        lecture = str(request.data.get('lecture') or '').strip()
        comment = str(request.data.get('comment') or '').strip()
        rating = float(request.data.get('rating') or 0.0)
        if not lecture:
            return Response({'success': False, 'error': 'lecture is required'}, status=400)
        if rating <= 0 or rating > 5:
            return Response({'success': False, 'error': 'rating must be between 0 and 5'}, status=400)

        feedback = LectureFeedback.objects.create(lecture_title=lecture, comment=comment, rating=rating)
        return Response({'success': True, 'feedback': {
            'id': feedback.id,
            'lecture': feedback.lecture_title,
            'rating': round(float(feedback.rating or 0.0), 1),
            'comment': feedback.comment,
            'submittedAt': feedback.submitted_at.isoformat(),
        }})
    except Exception as e:
        logger.error(f"Add feedback error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_notifications(request):
    """Get all notifications for the current teacher"""
    try:
        notifications = Notification.objects.all().order_by('-created_at')
        unread_count = notifications.filter(is_read=False).count()
        
        data = {
            'success': True,
            'total': notifications.count(),
            'unread': unread_count,
            'notifications': [
                {
                    'id': n.id,
                    'type': n.type,
                    'message': n.message,
                    'is_read': n.is_read,
                    'created_at': n.created_at.isoformat(),
                    'student_id': n.related_student.student_id if n.related_student else None,
                    'student_name': n.related_student.name if n.related_student else None,
                }
                for n in notifications[:100]  # Latest 100
            ]
        }
        return Response(data)
    except Exception as e:
        logger.error(f"Notifications error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def api_mark_notification_read(request):
    """Mark notification(s) as read"""
    try:
        notification_id = request.data.get('notification_id')
        mark_all = request.data.get('mark_all', False)
        
        if mark_all:
            Notification.objects.update(is_read=True)
            return Response({'success': True, 'message': 'All notifications marked as read'})
        elif notification_id:
            notif = Notification.objects.get(id=notification_id)
            notif.is_read = True
            notif.save()
            return Response({'success': True, 'message': f'Notification {notification_id} marked as read'})
        else:
            return Response({'success': False, 'error': 'No notification_id or mark_all provided'}, status=400)
    except Notification.DoesNotExist:
        return Response({'success': False, 'error': 'Notification not found'}, status=404)
    except Exception as e:
        logger.error(f"Mark notification read error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_ai_insights(request):
    """Get AI insights for all students"""
    try:
        student_id = request.query_params.get('student_id')
        
        if student_id:
            # Get insights for specific student
            insights = AIInsight.objects.filter(student__student_id=student_id).order_by('-week_start_date')
        else:
            # Get all insights
            insights = AIInsight.objects.all().order_by('-week_start_date')
        
        data = {
            'success': True,
            'total': insights.count(),
            'insights': [
                {
                    'id': i.id,
                    'student_id': i.student.student_id,
                    'student_name': i.student.name,
                    'week_start_date': i.week_start_date.isoformat(),
                    'engagement_trend': i.get_engagement_trend(),
                    'risk_level': i.risk_level,
                    'recommendation_text': i.recommendation_text,
                    'generated_at': i.generated_at.isoformat(),
                }
                for i in insights[:50]
            ]
        }
        return Response(data)
    except Exception as e:
        logger.error(f"AI insights error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_ai_insights_by_student(request, student_id):
    """Get AI insights for a single student ID"""
    try:
        insights = AIInsight.objects.filter(student__student_id=student_id).order_by('-week_start_date')
        if not insights.exists():
            return Response({'success': True, 'student_id': student_id, 'insights': [], 'total': 0})

        return Response({
            'success': True,
            'student_id': student_id,
            'total': insights.count(),
            'insights': [
                {
                    'id': i.id,
                    'student_id': i.student.student_id,
                    'student_name': i.student.name,
                    'week_start_date': i.week_start_date.isoformat(),
                    'engagement_trend': i.get_engagement_trend(),
                    'risk_level': i.risk_level,
                    'recommendation_text': i.recommendation_text,
                    'generated_at': i.generated_at.isoformat(),
                }
                for i in insights
            ]
        })
    except Exception as e:
        logger.error(f"AI insights by student error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_students_at_risk(request):
    """Get students with high risk level"""
    try:
        threshold_days = int(request.query_params.get('threshold_days', 3))
        
        # Students with risk_level = high
        at_risk_students = Student.objects.filter(risk_level='high')
        
        # Build data with engagement history
        data = {
            'success': True,
            'threshold_days': threshold_days,
            'total_at_risk': at_risk_students.count(),
            'students': []
        }
        
        for student in at_risk_students:
            # Get recent engagement records
            recent_records = EngagementRecord.objects.filter(
                student=student
            ).order_by('-timestamp')[:100]
            
            avg_engagement = 0
            if recent_records:
                avg_engagement = sum(r.engagement_score for r in recent_records) / len(recent_records)
            
            data['students'].append({
                'student_id': student.student_id,
                'name': student.name,
                'email': student.email,
                'risk_level': student.risk_level,
                'avg_engagement': round(avg_engagement, 2),
                'recent_engagement_count': len(recent_records),
            })
        
        return Response(data)
    except Exception as e:
        logger.error(f"At-risk students error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def api_attendance_bulk_mark(request):
    """Mark attendance for multiple students in a session"""
    try:
        session_id = request.data.get('session_id')
        attendance_data = request.data.get('attendance_data', [])  # [{'student_id': '...', 'is_present': bool}, ...]
        
        if not session_id:
            return Response({'success': False, 'error': 'session_id required'}, status=400)
        
        session = ClassSession.objects.get(id=session_id)
        today = timezone.now().date()
        marked_count = 0
        
        for item in attendance_data:
            student_id = item.get('student_id')
            is_present = item.get('is_present', False)
            
            try:
                student = Student.objects.get(student_id=student_id)
                attendance, created = Attendance.objects.get_or_create(
                    student=student,
                    session=session,
                    date=today,
                    defaults={'is_present': is_present}
                )
                if not created:
                    attendance.is_present = is_present
                    attendance.save()
                if not is_present:
                    # If explicitly marked absent in bulk, evaluate low attendance trend quickly.
                    student_total = Attendance.objects.filter(student=student).count()
                    student_present = Attendance.objects.filter(student=student, is_present=True).count()
                    if student_total > 0:
                        attendance_rate = (student_present / student_total) * 100
                        if attendance_rate < 50:
                            _create_notification_if_needed(
                                'alert',
                                f'Attendance below 50% for {student.name} ({attendance_rate:.1f}%).',
                                related_student=student,
                                dedupe_hours=24,
                            )
                marked_count += 1
            except Student.DoesNotExist:
                continue
        
        return Response({
            'success': True,
            'message': f'Marked attendance for {marked_count} students',
            'marked': marked_count
        })
    except ClassSession.DoesNotExist:
        return Response({'success': False, 'error': 'Session not found'}, status=404)
    except Exception as e:
        logger.error(f"Bulk mark attendance error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_dashboard_summary(request):
    """Get summary data for all dashboard stat cards"""
    try:
        today = timezone.now().date()
        engagement_today = timezone.localdate()
        session_id = request.query_params.get('session_id')
        
        # Get today's session if not specified
        if not session_id:
            session = ClassSession.objects.filter(
                status='active',
                start_time__date=today
            ).first()
        else:
            session = ClassSession.objects.get(id=session_id) if session_id else None
        
        # Average Class Engagement Today
        today_records = EngagementRecord.objects.filter(
            timestamp__date=engagement_today
        )
        avg_engagement_today = 0
        if today_records:
            avg_engagement_today = today_records.aggregate(
                avg=Avg('engagement_score')
            )['avg'] or 0
        
        # Total Alerts This Week
        week_start = today - timedelta(days=7)
        alerts_this_week = Alert.objects.filter(
            timestamp__date__gte=week_start,
            timestamp__date__lte=today
        ).count()
        
        # Students At Risk
        at_risk_count = Student.objects.filter(risk_level='high').count()
        
        # Syllabus Completion %
        total_topics = SyllabusTopic.objects.count()
        completed_topics = SyllabusTopic.objects.filter(status='completed').count()
        syllabus_completion = 0
        if total_topics > 0:
            syllabus_completion = (completed_topics / total_topics) * 100
        
        data = {
            'success': True,
            'date': today.isoformat(),
            'summary_stats': {
                'avg_engagement_today': round(avg_engagement_today, 2),
                'total_alerts_week': alerts_this_week,
                'students_at_risk': at_risk_count,
                'syllabus_completion_percent': round(syllabus_completion, 2),
            },
            'session_info': {
                'session_id': session.id if session else None,
                'session_active': session.status == 'active' if session else False,
                'session_start': session.start_time.isoformat() if session else None,
            }
        }
        
        return Response(data)
    except Exception as e:
        logger.error(f"Dashboard summary error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def database_tables_overview(request):
    try:
        tables = {
            'teachers': {'count': Teacher.objects.count()},
            'students': {'count': Student.objects.count()},
            'class_sessions': {'count': ClassSession.objects.count()},
            'syllabus_topics': {'count': SyllabusTopic.objects.count()},
            'daily_lecture_topics': {'count': DailyLectureTopic.objects.count()},
            'student_topic_progress': {'count': StudentTopicProgress.objects.count()},
            'extra_lecture_plans': {'count': ExtraLecturePlan.objects.count()},
            'lecture_feedback': {'count': LectureFeedback.objects.count()},
            'attendance': {'count': Attendance.objects.count()},
            'engagement_records': {'count': EngagementRecord.objects.count()},
            'reports': {'count': Report.objects.count()},
        }
        return Response({'success': True, 'tables': tables})
    except Exception as e:
        logger.error(f"Database tables overview error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


# ─── Teacher Module V2 APIs ────────────────────────────────────────────────

def _fmt_date_indian(value):
    if not value:
        return None
    try:
        return value.strftime('%d-%m-%Y')
    except Exception:
        return None


def _seed_syllabus_if_empty(teacher):
    if Syllabus.objects.filter(teacher=teacher).exists():
        return

    today = timezone.localdate()
    seed = [
        ('Computer Science', 'Unit 1', 'Introduction to Programming', 2.0, 0, 'high', 'completed'),
        ('Computer Science', 'Unit 1', 'Control Flow and Loops', 2.5, 1, 'high', 'in_progress'),
        ('Computer Science', 'Unit 2', 'Functions and Modules', 3.0, 3, 'medium', 'pending'),
        ('Computer Science', 'Unit 2', 'Data Structures Basics', 2.5, 5, 'medium', 'pending'),
        ('Computer Science', 'Unit 3', 'File Handling', 1.5, 7, 'low', 'pending'),
    ]

    for subject, unit, topic, est_hours, day_offset, priority, status_code in seed:
        Syllabus.objects.create(
            teacher=teacher,
            subject=subject,
            unit=unit,
            topic=topic,
            estimated_hours=est_hours,
            target_date=today + timedelta(days=day_offset),
            priority=priority,
            status=status_code,
        )


def _serialize_syllabus(item):
    delayed = bool(item.target_date and item.target_date < timezone.localdate() and item.status != 'completed')
    return {
        'id': item.id,
        'subject': item.subject,
        'unit': item.unit,
        'topic': item.topic,
        'estimated_hours': float(item.estimated_hours or 0),
        'target_date': _fmt_date_indian(item.target_date),
        'priority': item.priority,
        'status': item.status,
        'is_delayed': delayed,
        'auto_healing_date': _fmt_date_indian(item.auto_healing_date),
        'is_auto_healed': bool(item.is_auto_healed),
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
    }


def _log_teacher_activity(teacher, action_text):
    if not teacher:
        return
    ActivityLog.objects.create(teacher=teacher, action_text=action_text[:255])


def _risk_level_from_metrics(engagement, attendance, pass_rate):
    if engagement < 50 or attendance < 60 or pass_rate < 50:
        return 'high'
    if engagement < 70 or attendance < 75 or pass_rate < 70:
        return 'medium'
    return 'low'


@api_view(['GET', 'POST'])
@authentication_classes([])
@permission_classes([])
def api_syllabus(request):
    try:
        teacher = _get_request_teacher(request)
        _seed_syllabus_if_empty(teacher)

        if request.method == 'POST':
            subject = str(request.data.get('subject') or '').strip()
            unit = str(request.data.get('unit') or '').strip()
            topic = str(request.data.get('topic') or '').strip()
            if not subject or not unit or not topic:
                return Response({'success': False, 'error': 'subject, unit and topic are required'}, status=400)

            item = Syllabus.objects.create(
                teacher=teacher,
                subject=subject,
                unit=unit,
                topic=topic,
                estimated_hours=float(request.data.get('estimated_hours') or 1.0),
                target_date=datetime.strptime(request.data.get('target_date'), '%Y-%m-%d').date() if request.data.get('target_date') else timezone.localdate(),
                priority=str(request.data.get('priority') or 'medium'),
                status=str(request.data.get('status') or 'pending'),
            )
            return Response({'success': True, 'item': _serialize_syllabus(item)})

        qs = Syllabus.objects.filter(teacher=teacher).order_by('target_date', '-created_at')
        subject = request.query_params.get('subject')
        status_filter = request.query_params.get('status')
        priority = request.query_params.get('priority')
        if subject:
            qs = qs.filter(subject=subject)
        if status_filter:
            qs = qs.filter(status=status_filter)
        if priority:
            qs = qs.filter(priority=priority)

        items = list(qs)
        subjects = sorted(Syllabus.objects.filter(teacher=teacher).values_list('subject', flat=True).distinct())
        units = sorted(Syllabus.objects.filter(teacher=teacher).values_list('unit', flat=True).distinct())
        return Response({'success': True, 'items': [_serialize_syllabus(i) for i in items], 'subjects': subjects, 'units': units})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['PUT', 'DELETE'])
@authentication_classes([])
@permission_classes([])
def api_syllabus_detail(request, syllabus_id):
    try:
        teacher = _get_request_teacher(request)
        item = Syllabus.objects.get(id=syllabus_id, teacher=teacher)
        if request.method == 'DELETE':
            item.delete()
            return Response({'success': True, 'message': 'Topic deleted'})

        for field in ['subject', 'unit', 'topic', 'priority', 'status']:
            if field in request.data:
                setattr(item, field, str(request.data.get(field) or '').strip())
        if 'estimated_hours' in request.data:
            item.estimated_hours = float(request.data.get('estimated_hours') or 0.0)
        if 'target_date' in request.data and request.data.get('target_date'):
            item.target_date = datetime.strptime(request.data.get('target_date'), '%Y-%m-%d').date()
        item.save()
        return Response({'success': True, 'item': _serialize_syllabus(item)})
    except Syllabus.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_syllabus_progress(request):
    try:
        teacher = _get_request_teacher(request)
        total = Syllabus.objects.filter(teacher=teacher).count()
        pending = Syllabus.objects.filter(teacher=teacher, status='pending').count()
        in_progress = Syllabus.objects.filter(teacher=teacher, status='in_progress').count()
        completed = Syllabus.objects.filter(teacher=teacher, status='completed').count()
        completion_pct = round((completed / total) * 100.0, 1) if total else 0.0
        return Response({
            'success': True,
            'completion_percent': completion_pct,
            'distribution': {
                'pending': pending,
                'in_progress': in_progress,
                'completed': completed,
            },
        })
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_syllabus_delayed(request):
    try:
        teacher = _get_request_teacher(request)
        today = timezone.localdate()
        delayed = Syllabus.objects.filter(teacher=teacher, target_date__lt=today).exclude(status='completed').order_by('target_date')
        rows = []
        for item in delayed:
            days = (today - item.target_date).days
            rows.append({
                'id': item.id,
                'topic': item.topic,
                'subject': item.subject,
                'unit': item.unit,
                'target_date': _fmt_date_indian(item.target_date),
                'days_delayed': int(days),
                'severity': 'red' if days > 7 else ('orange' if days >= 3 else 'yellow'),
                'auto_healing_date': _fmt_date_indian(item.auto_healing_date),
            })
        return Response({'success': True, 'items': rows})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_syllabus_auto_heal(request):
    try:
        teacher = _get_request_teacher(request)
        today = timezone.localdate()
        delayed = list(Syllabus.objects.filter(teacher=teacher, target_date__lt=today).exclude(status='completed').order_by('target_date'))
        cursor = today + timedelta(days=1)
        suggestions = []
        for item in delayed:
            item.auto_healing_date = cursor
            item.save(update_fields=['auto_healing_date'])
            suggestions.append({
                'id': item.id,
                'topic': item.topic,
                'old_date': _fmt_date_indian(item.target_date),
                'suggested_date': _fmt_date_indian(cursor),
                'estimated_hours': float(item.estimated_hours or 0),
            })
            step = max(1, int(round((item.estimated_hours or 1.0) / 2.0)))
            cursor = cursor + timedelta(days=step)
        return Response({'success': True, 'suggestions': suggestions})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def api_syllabus_auto_heal_accept(request):
    try:
        teacher = _get_request_teacher(request)
        qs = Syllabus.objects.filter(teacher=teacher, auto_healing_date__isnull=False).exclude(status='completed')
        count = 0
        for item in qs:
            item.target_date = item.auto_healing_date
            item.is_auto_healed = True
            item.save(update_fields=['target_date', 'is_auto_healed'])
            count += 1
        return Response({'success': True, 'updated': count})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def api_syllabus_reschedule(request, syllabus_id):
    try:
        teacher = _get_request_teacher(request)
        item = Syllabus.objects.get(id=syllabus_id, teacher=teacher)
        target_date = request.data.get('target_date')
        if not target_date:
            return Response({'success': False, 'error': 'target_date is required'}, status=400)
        item.target_date = datetime.strptime(target_date, '%Y-%m-%d').date()
        item.is_auto_healed = False
        item.save(update_fields=['target_date', 'is_auto_healed'])
        return Response({'success': True, 'item': _serialize_syllabus(item)})
    except Syllabus.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


def _serialize_lecture_plan(plan):
    return {
        'id': plan.id,
        'topic_id': plan.topic_id,
        'topic': plan.topic.topic,
        'subject': plan.topic.subject,
        'unit': plan.topic.unit,
        'lecture_date': _fmt_date_indian(plan.lecture_date),
        'start_time': plan.start_time.strftime('%H:%M') if plan.start_time else None,
        'end_time': plan.end_time.strftime('%H:%M') if plan.end_time else None,
        'notes': plan.notes,
        'status': plan.status,
    }


@api_view(['GET', 'POST'])
@authentication_classes([])
@permission_classes([])
def api_lecture_plan(request):
    try:
        teacher = _get_request_teacher(request)
        today = timezone.localdate()
        if request.method == 'POST':
            topic_id = request.data.get('topic_id')
            if not topic_id:
                return Response({'success': False, 'error': 'topic_id is required'}, status=400)
            topic = Syllabus.objects.get(id=topic_id, teacher=teacher)
            lecture_date = datetime.strptime(request.data.get('lecture_date'), '%Y-%m-%d').date() if request.data.get('lecture_date') else today
            start_time = datetime.strptime(request.data.get('start_time'), '%H:%M').time() if request.data.get('start_time') else None
            end_time = datetime.strptime(request.data.get('end_time'), '%H:%M').time() if request.data.get('end_time') else None
            plan = LecturePlan.objects.create(
                teacher=teacher,
                topic=topic,
                lecture_date=lecture_date,
                start_time=start_time,
                end_time=end_time,
                notes=str(request.data.get('notes') or '').strip(),
                status='planned',
            )
            return Response({'success': True, 'plan': _serialize_lecture_plan(plan)})

        plans = LecturePlan.objects.filter(teacher=teacher, lecture_date=today).select_related('topic').order_by('start_time', 'created_at')
        planned_hours = 0.0
        completed_hours = 0.0
        for p in plans:
            if p.start_time and p.end_time:
                diff = datetime.combine(today, p.end_time) - datetime.combine(today, p.start_time)
                hours = max(0.0, diff.total_seconds() / 3600.0)
                planned_hours += hours
                if p.status == 'done':
                    completed_hours += hours
        return Response({
            'success': True,
            'plans': [_serialize_lecture_plan(p) for p in plans],
            'duration_tracker': {
                'planned_hours': round(planned_hours, 1),
                'completed_hours': round(completed_hours, 1),
            },
        })
    except Syllabus.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['PUT', 'DELETE'])
@authentication_classes([])
@permission_classes([])
def api_lecture_plan_detail(request, plan_id):
    try:
        teacher = _get_request_teacher(request)
        plan = LecturePlan.objects.select_related('topic').get(id=plan_id, teacher=teacher)
        if request.method == 'DELETE':
            plan.delete()
            return Response({'success': True, 'message': 'Plan removed'})

        if 'status' in request.data:
            new_status = str(request.data.get('status') or '').strip()
            if new_status not in {'planned', 'done', 'skipped', 'in_progress'}:
                return Response({'success': False, 'error': 'Invalid status'}, status=400)
            plan.status = new_status
            plan.save(update_fields=['status'])
            if new_status == 'done':
                topic = plan.topic
                topic.status = 'completed'
                topic.save(update_fields=['status'])
                _log_teacher_activity(teacher, f"Marked {topic.topic} as Done")
        for field in ['notes']:
            if field in request.data:
                setattr(plan, field, str(request.data.get(field) or '').strip())
        if 'start_time' in request.data and request.data.get('start_time'):
            plan.start_time = datetime.strptime(request.data.get('start_time'), '%H:%M').time()
        if 'end_time' in request.data and request.data.get('end_time'):
            plan.end_time = datetime.strptime(request.data.get('end_time'), '%H:%M').time()
        plan.save()
        return Response({'success': True, 'plan': _serialize_lecture_plan(plan), 'checkpoint_prompt': plan.status == 'done'})
    except LecturePlan.DoesNotExist:
        return Response({'success': False, 'error': 'Plan not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_lecture_plan_history(request):
    try:
        teacher = _get_request_teacher(request)
        today = timezone.localdate()
        plans = LecturePlan.objects.filter(teacher=teacher, lecture_date__lt=today).select_related('topic').order_by('-lecture_date', '-created_at')[:100]
        return Response({'success': True, 'history': [_serialize_lecture_plan(p) for p in plans]})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


def _serialize_checkpoint(cp):
    result_qs = cp.results.all()
    avg_score = result_qs.aggregate(v=Avg('score')).get('v') or 0.0
    total = result_qs.count()
    passed = result_qs.filter(passed=True).count()
    pass_rate = (passed / total * 100.0) if total else 0.0
    return {
        'id': cp.id,
        'topic_id': cp.topic_id,
        'topic': cp.topic.topic,
        'type': cp.checkpoint_type,
        'title': cp.title,
        'passing_score': cp.passing_score,
        'deadline': _fmt_date_indian(cp.deadline),
        'assigned_date': _fmt_date_indian(cp.created_at.date()),
        'avg_score': round(float(avg_score), 1),
        'pass_rate': round(float(pass_rate), 1),
        'status': 'closed' if cp.deadline < timezone.localdate() else 'active',
    }


@api_view(['GET', 'POST'])
@authentication_classes([])
@permission_classes([])
def api_checkpoints(request):
    try:
        teacher = _get_request_teacher(request)
        if request.method == 'POST':
            topic = Syllabus.objects.get(id=request.data.get('topic_id'), teacher=teacher)
            cp = Checkpoint.objects.create(
                topic=topic,
                title=str(request.data.get('title') or '').strip() or f"Checkpoint - {topic.topic}",
                checkpoint_type=str(request.data.get('checkpoint_type') or 'mcq'),
                passing_score=int(request.data.get('passing_score') or 60),
                deadline=datetime.strptime(request.data.get('deadline'), '%Y-%m-%d').date() if request.data.get('deadline') else timezone.localdate() + timedelta(days=7),
            )
            _log_teacher_activity(teacher, f"Created checkpoint {cp.title}")
            return Response({'success': True, 'checkpoint': _serialize_checkpoint(cp)})

        checkpoints = Checkpoint.objects.filter(topic__teacher=teacher).select_related('topic').order_by('-created_at')
        return Response({'success': True, 'checkpoints': [_serialize_checkpoint(c) for c in checkpoints]})
    except Syllabus.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['PUT', 'DELETE'])
@authentication_classes([])
@permission_classes([])
def api_checkpoint_detail(request, checkpoint_id):
    try:
        teacher = _get_request_teacher(request)
        cp = Checkpoint.objects.select_related('topic').get(id=checkpoint_id, topic__teacher=teacher)
        if request.method == 'DELETE':
            cp.delete()
            return Response({'success': True, 'message': 'Checkpoint deleted'})
        for field in ['title', 'checkpoint_type']:
            if field in request.data:
                setattr(cp, field, str(request.data.get(field) or '').strip())
        if 'passing_score' in request.data:
            cp.passing_score = int(request.data.get('passing_score') or 60)
        if 'deadline' in request.data and request.data.get('deadline'):
            cp.deadline = datetime.strptime(request.data.get('deadline'), '%Y-%m-%d').date()
        cp.save()
        return Response({'success': True, 'checkpoint': _serialize_checkpoint(cp)})
    except Checkpoint.DoesNotExist:
        return Response({'success': False, 'error': 'Checkpoint not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_checkpoint_results(request, checkpoint_id):
    try:
        teacher = _get_request_teacher(request)
        cp = Checkpoint.objects.get(id=checkpoint_id, topic__teacher=teacher)
        results = CheckpointResult.objects.filter(checkpoint=cp).select_related('student').order_by('-attempted_at')
        rows = [{
            'student_id': r.student.student_id,
            'student_name': r.student.name,
            'score': round(float(r.score), 1),
            'passed': bool(r.passed),
            'attempted_at': r.attempted_at.isoformat(),
        } for r in results]
        return Response({'success': True, 'checkpoint': _serialize_checkpoint(cp), 'results': rows})
    except Checkpoint.DoesNotExist:
        return Response({'success': False, 'error': 'Checkpoint not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def api_checkpoint_send_reminder(request):
    try:
        teacher = _get_request_teacher(request)
        checkpoint_id = request.data.get('checkpoint_id')
        if not checkpoint_id:
            return Response({'success': False, 'error': 'checkpoint_id is required'}, status=400)
        cp = Checkpoint.objects.select_related('topic').get(id=checkpoint_id, topic__teacher=teacher)

        attempted_ids = set(CheckpointResult.objects.filter(checkpoint=cp).values_list('student_id', flat=True))
        target_students = Student.objects.exclude(id__in=list(attempted_ids))
        for student in target_students:
            Notification.objects.create(
                type='info',
                message=f"Reminder: complete checkpoint '{cp.title}' before {_fmt_date_indian(cp.deadline)}",
                related_student=student,
            )
        return Response({'success': True, 'reminders_sent': target_students.count()})
    except Checkpoint.DoesNotExist:
        return Response({'success': False, 'error': 'Checkpoint not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_checkpoint_summary(request):
    try:
        teacher = _get_request_teacher(request)
        cps = Checkpoint.objects.filter(topic__teacher=teacher)
        result_qs = CheckpointResult.objects.filter(checkpoint__in=cps)
        avg_score = result_qs.aggregate(v=Avg('score')).get('v') or 0.0
        total = result_qs.count()
        passed = result_qs.filter(passed=True).count()
        pass_rate = (passed / total * 100.0) if total else 0.0
        return Response({'success': True, 'class_avg': round(float(avg_score), 1), 'pass_rate': round(float(pass_rate), 1), 'total_attempts': total})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)
@authentication_classes([])
@permission_classes([])
def api_students_lagging(request):
    try:
        teacher = _get_request_teacher(request)
        students = Student.objects.filter(is_active=True).order_by('name')
        rows = []
        for student in students:
            eng = EngagementRecord.objects.filter(student=student).aggregate(v=Avg('engagement_score')).get('v') or 0.0
            att_total = Attendance.objects.filter(student=student).count()
            att_present = Attendance.objects.filter(student=student, is_present=True).count()
            attendance_pct = (att_present / att_total * 100.0) if att_total else 0.0
            
            # Check if student is lagging (more reasonable thresholds)
            if eng < 70 or attendance_pct < 75:
                # Fix: Use SyllabusTopic instead of Syllabus
                lag_topic = SyllabusTopic.objects.filter(teacher=teacher, status__in=['pending', 'in_progress']).order_by('planned_date').first()
                rows.append({
                    'student_id': student.student_id,
                    'student_name': student.name,
                    'roll_no': student.student_id,
                    'lagging_topic': lag_topic.topic if lag_topic else 'General support',
                    'topic_id': lag_topic.id if lag_topic else None,
                    'engagement_percent': round(float(eng), 1),
                    'attendance_percent': round(float(attendance_pct), 1),
                    'risk_level': student.risk_level,
                })
        return Response({'success': True, 'students': rows})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


def _serialize_extra_lecture(row):
    return {
        'id': row.id,
        'student_id': row.student.student_id,
        'student_name': row.student.name,
        'topic_id': row.topic_id,
        'topic': row.topic.topic,
        'scheduled_date': _fmt_date_indian(row.scheduled_date),
        'scheduled_time': row.scheduled_time.strftime('%H:%M') if row.scheduled_time else None,
        'notes': row.notes,
        'status': row.status,
    }


@api_view(['GET', 'POST'])
@authentication_classes([])
@permission_classes([])
def api_extra_lectures(request):
    try:
        teacher = _get_request_teacher(request)
        if request.method == 'POST':
            student = Student.objects.get(student_id=request.data.get('student_id'))
            topic = Syllabus.objects.get(id=request.data.get('topic_id'), teacher=teacher)
            scheduled_date = datetime.strptime(request.data.get('scheduled_date'), '%Y-%m-%d').date() if request.data.get('scheduled_date') else timezone.localdate() + timedelta(days=1)
            scheduled_time = datetime.strptime(request.data.get('scheduled_time'), '%H:%M').time() if request.data.get('scheduled_time') else None
            row = ExtraLecture.objects.create(
                teacher=teacher,
                student=student,
                topic=topic,
                scheduled_date=scheduled_date,
                scheduled_time=scheduled_time,
                notes=str(request.data.get('notes') or '').strip(),
                status='scheduled',
            )
            _log_teacher_activity(teacher, f"Scheduled extra lecture for {student.name}")
            return Response({'success': True, 'extra_lecture': _serialize_extra_lecture(row)})

        rows = ExtraLecture.objects.filter(teacher=teacher).select_related('student', 'topic').order_by('-scheduled_date', '-created_at')
        return Response({'success': True, 'items': [_serialize_extra_lecture(r) for r in rows]})
    except (Student.DoesNotExist, Syllabus.DoesNotExist):
        return Response({'success': False, 'error': 'Student or topic not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['PUT', 'DELETE'])
@authentication_classes([])
@permission_classes([])
def api_extra_lecture_detail(request, lecture_id):
    try:
        teacher = _get_request_teacher(request)
        row = ExtraLecture.objects.select_related('student', 'topic').get(id=lecture_id, teacher=teacher)
        if request.method == 'DELETE':
            row.status = 'cancelled'
            row.save(update_fields=['status'])
            return Response({'success': True, 'message': 'Extra lecture cancelled'})
        if 'status' in request.data:
            row.status = str(request.data.get('status') or 'scheduled')
        if 'scheduled_date' in request.data and request.data.get('scheduled_date'):
            row.scheduled_date = datetime.strptime(request.data.get('scheduled_date'), '%Y-%m-%d').date()
        if 'scheduled_time' in request.data and request.data.get('scheduled_time'):
            row.scheduled_time = datetime.strptime(request.data.get('scheduled_time'), '%H:%M').time()
        if 'notes' in request.data:
            row.notes = str(request.data.get('notes') or '').strip()
        row.save()
        return Response({'success': True, 'extra_lecture': _serialize_extra_lecture(row)})
    except ExtraLecture.DoesNotExist:
        return Response({'success': False, 'error': 'Extra lecture not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def api_extra_lecture_send_note(request):
    try:
        student = Student.objects.get(student_id=request.data.get('student_id'))
        note = str(request.data.get('note') or '').strip() or 'Please attend your extra support lecture.'
        Notification.objects.create(type='info', message=note, related_student=student)
        return Response({'success': True, 'message': 'Note sent'})
    except Student.DoesNotExist:
        return Response({'success': False, 'error': 'Student not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_students_performance(request):
    try:
        teacher = _get_request_teacher(request)
        students = Student.objects.filter(is_active=True).order_by('name')
        items = []
        for s in students:
            eng = EngagementRecord.objects.filter(student=s).aggregate(v=Avg('engagement_score')).get('v') or 0.0
            att_total = Attendance.objects.filter(student=s).count()
            att_present = Attendance.objects.filter(student=s, is_present=True).count()
            attendance = (att_present / att_total * 100.0) if att_total else 0.0
            
            # Fix: Use StudentTopicProgress for individual student completion
            student_topics = StudentTopicProgress.objects.filter(student__student_id=s.student_id, topic__teacher=teacher)
            completion = student_topics.aggregate(avg=Avg('completion_percent')).get('avg') or 0.0

            student_results = CheckpointResult.objects.filter(student=s)
            pass_rate = (student_results.filter(passed=True).count() / student_results.count() * 100.0) if student_results.exists() else 100.0
            risk = _risk_level_from_metrics(eng, attendance, pass_rate)
            if s.risk_level != risk:
                s.risk_level = risk
                s.save(update_fields=['risk_level'])

            items.append({
                'id': s.id,
                'student_id': s.student_id,
                'name': s.name,
                'roll_no': s.student_id,
                'engagement_percent': round(float(eng), 1),
                'attendance_percent': round(float(attendance), 1),
                'topic_completion_percent': round(float(completion), 1),
                'risk_level': risk,
                'profile_photo': s.profile_photo.url if s.profile_photo else None,
            })
        return Response({'success': True, 'students': items})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_student_performance_detail(request, student_pk):
    try:
        student = Student.objects.get(id=student_pk)
        trend = list(
            EngagementRecord.objects.filter(student=student).order_by('-timestamp')[:30].values('timestamp', 'engagement_score')
        )
        trend.reverse()
        checkpoint_scores = []
        for row in CheckpointResult.objects.filter(student=student).select_related('checkpoint', 'checkpoint__topic').order_by('-attempted_at')[:50]:
            checkpoint_scores.append({
                'topic': row.checkpoint.topic.topic,
                'checkpoint': row.checkpoint.title,
                'score': round(float(row.score), 1),
                'passed': bool(row.passed),
            })
        return Response({
            'success': True,
            'student': {
                'student_id': student.student_id,
                'name': student.name,
                'risk_level': student.risk_level,
            },
            'engagement_trend': [
                {
                    'date': _fmt_date_indian(t['timestamp'].date()),
                    'engagement': round(float(t['engagement_score']), 1),
                }
                for t in trend
            ],
            'checkpoint_scores': checkpoint_scores,
        })
    except Student.DoesNotExist:
        return Response({'success': False, 'error': 'Student not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_student_engagement_trend(request, student_pk):
    try:
        student = Student.objects.get(id=student_pk)
        since = timezone.now() - timedelta(days=30)
        rows = EngagementRecord.objects.filter(student=student, timestamp__gte=since).order_by('timestamp')
        data = [{'date': _fmt_date_indian(r.timestamp.date()), 'engagement': round(float(r.engagement_score), 1)} for r in rows]
        return Response({'success': True, 'student_id': student.student_id, 'trend': data})
    except Student.DoesNotExist:
        return Response({'success': False, 'error': 'Student not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_student_attendance_calendar(request, student_pk):
    try:
        student = Student.objects.get(id=student_pk)
        rows = Attendance.objects.filter(student=student).order_by('-date')[:90]
        data = [{'date': _fmt_date_indian(r.date), 'present': bool(r.is_present)} for r in rows]
        return Response({'success': True, 'student_id': student.student_id, 'calendar': data})
    except Student.DoesNotExist:
        return Response({'success': False, 'error': 'Student not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_student_ai_recommendation(request, student_pk):
    try:
        student = Student.objects.get(id=student_pk)
        eng_rows = list(EngagementRecord.objects.filter(student=student).order_by('-timestamp')[:10])
        low_days = sum(1 for r in eng_rows if float(r.engagement_score or 0) < 50)
        att_total = Attendance.objects.filter(student=student).count()
        att_present = Attendance.objects.filter(student=student, is_present=True).count()
        attendance = (att_present / att_total * 100.0) if att_total else 100.0
        cp_qs = CheckpointResult.objects.filter(student=student)
        pass_rate = (cp_qs.filter(passed=True).count() / cp_qs.count() * 100.0) if cp_qs.exists() else 100.0

        if low_days >= 5:
            msg = 'Student shows consistent disengagement. Recommend one-on-one session.'
        elif attendance < 60:
            msg = 'Frequent absences detected. Contact student/parents.'
        elif pass_rate < 50:
            msg = 'Student struggling with topics. Schedule revision lecture.'
        else:
            msg = 'Student is performing well. Keep it up!'

        return Response({'success': True, 'student_id': student.student_id, 'recommendation': msg})
    except Student.DoesNotExist:
        return Response({'success': False, 'error': 'Student not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET', 'POST'])
@authentication_classes([])
@permission_classes([])
def api_feedback_v2(request):
    try:
        if request.method == 'POST':
            lecture = None
            if request.data.get('lecture_id'):
                lecture = LecturePlan.objects.filter(id=request.data.get('lecture_id')).first()
            student = Student.objects.filter(student_id=request.data.get('student_id')).first() if request.data.get('student_id') else None
            fb = Feedback.objects.create(
                lecture=lecture,
                student=student,
                rating=int(request.data.get('rating') or 5),
                comment=str(request.data.get('comment') or '').strip(),
                is_anonymous=bool(request.data.get('is_anonymous', False)),
            )
            return Response({'success': True, 'feedback_id': fb.id})

        qs = Feedback.objects.select_related('lecture', 'lecture__topic', 'student').order_by('-created_at')
        rating = request.query_params.get('rating')
        if rating:
            qs = qs.filter(rating=int(rating))
        items = []
        for f in qs[:300]:
            lecture_title = f.lecture.topic.topic if f.lecture and f.lecture.topic else 'General Lecture'
            items.append({
                'id': f.id,
                'lecture': lecture_title,
                'rating': f.rating,
                'comment': f.comment,
                'date': _fmt_date_indian(f.created_at.date()),
                'student_name': 'Anonymous' if f.is_anonymous else (f.student.name if f.student else 'Unknown'),
                'is_anonymous': bool(f.is_anonymous),
                'teacher_reply': f.teacher_reply,
            })
        return Response({'success': True, 'items': items})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['PUT'])
@authentication_classes([])
@permission_classes([])
def api_feedback_reply(request, feedback_id):
    try:
        fb = Feedback.objects.get(id=feedback_id)
        fb.teacher_reply = str(request.data.get('teacher_reply') or '').strip()
        fb.save(update_fields=['teacher_reply'])
        return Response({'success': True})
    except Feedback.DoesNotExist:
        return Response({'success': False, 'error': 'Feedback not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['DELETE'])
@authentication_classes([])
@permission_classes([])
def api_feedback_delete(request, feedback_id):
    try:
        fb = Feedback.objects.get(id=feedback_id)
        fb.delete()
        return Response({'success': True})
    except Feedback.DoesNotExist:
        return Response({'success': False, 'error': 'Feedback not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_feedback_summary(request):
    try:
        qs = Feedback.objects.all()
        total = qs.count()
        avg = qs.aggregate(v=Avg('rating')).get('v') or 0.0
        dist = {str(i): qs.filter(rating=i).count() for i in [1, 2, 3, 4, 5]}

        keywords = Counter()
        for text in qs.values_list('comment', flat=True):
            for word in str(text or '').lower().split():
                clean = ''.join(ch for ch in word if ch.isalpha())
                if len(clean) >= 4:
                    keywords[clean] += 1

        common = keywords.most_common(1)[0][0] if keywords else ''
        return Response({'success': True, 'average_rating': round(float(avg), 2), 'distribution': dist, 'total_count': total, 'most_common_keyword': common})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_feedback_export(request):
    try:
        rows = Feedback.objects.select_related('lecture', 'lecture__topic', 'student').order_by('-created_at')
        output = []
        output.append(['Lecture', 'Rating', 'Comment', 'Date', 'Student'])
        for r in rows:
            lecture_title = r.lecture.topic.topic if r.lecture and r.lecture.topic else 'General Lecture'
            student_name = 'Anonymous' if r.is_anonymous else (r.student.name if r.student else 'Unknown')
            output.append([lecture_title, r.rating, r.comment, _fmt_date_indian(r.created_at.date()), student_name])

        csv_lines = []
        for row in output:
            escaped = ['"' + str(col).replace('"', '""') + '"' for col in row]
            csv_lines.append(','.join(escaped))
        csv_content = '\n'.join(csv_lines)
        return Response({'success': True, 'filename': 'feedback_export.csv', 'csv': csv_content})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


def _get_or_create_teacher_profile(teacher):
    user = User.objects.filter(email=teacher.email).first()
    if not user:
        username = teacher.email.split('@')[0]
        user = User.objects.create_user(username=username, email=teacher.email, password='demo123')
    profile, _ = TeacherProfile.objects.get_or_create(
        teacher=teacher,
        defaults={
            'user': user,
            'department': 'Computer Science',
            'employee_id': f"EMP-{teacher.id:04d}",
            'phone': '',
            'subjects': [teacher.subject] if teacher.subject else [],
        }
    )
    if profile.user_id != user.id:
        profile.user = user
        profile.save(update_fields=['user'])
    return profile


@api_view(['GET', 'PUT'])
@authentication_classes([])
@permission_classes([])
def api_teacher_profile(request):
    try:
        teacher = _get_request_teacher(request)
        profile = _get_or_create_teacher_profile(teacher)
        if request.method == 'PUT':
            teacher.name = str(request.data.get('name') or teacher.name)
            teacher.subject = str(request.data.get('subject') or teacher.subject)
            teacher.save(update_fields=['name', 'subject'])
            for field in ['department', 'employee_id', 'phone']:
                if field in request.data:
                    setattr(profile, field, str(request.data.get(field) or '').strip())
            if 'subjects' in request.data and isinstance(request.data.get('subjects'), list):
                profile.subjects = request.data.get('subjects')
            profile.save()

        return Response({'success': True, 'profile': {
            'name': teacher.name,
            'email': teacher.email,
            'department': profile.department,
            'employee_id': profile.employee_id,
            'phone': profile.phone,
            'subjects': profile.subjects or [],
            'profile_photo': profile.profile_photo.url if profile.profile_photo else None,
        }})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def api_teacher_profile_photo(request):
    try:
        teacher = _get_request_teacher(request)
        profile = _get_or_create_teacher_profile(teacher)
        photo = request.FILES.get('profile_photo')
        if not photo:
            return Response({'success': False, 'error': 'profile_photo file is required'}, status=400)
        profile.profile_photo = photo
        profile.save(update_fields=['profile_photo'])
        return Response({'success': True, 'profile_photo': profile.profile_photo.url})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_teacher_activity_log(request):
    try:
        teacher = _get_request_teacher(request)
        rows = ActivityLog.objects.filter(teacher=teacher).order_by('-created_at')[:20]
        return Response({'success': True, 'items': [{'action_text': r.action_text, 'created_at': r.created_at.isoformat()} for r in rows]})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def api_teacher_stats(request):
    try:
        teacher = _get_request_teacher(request)
        students_count = Attendance.objects.filter(session__teacher=teacher).values('student_id').distinct().count()
        avg_eng = EngagementRecord.objects.filter(session__teacher=teacher).aggregate(v=Avg('engagement_score')).get('v') or 0.0
        lectures_count = LecturePlan.objects.filter(teacher=teacher).count()
        total_topics = Syllabus.objects.filter(teacher=teacher).count()
        completed_topics = Syllabus.objects.filter(teacher=teacher, status='completed').count()
        syllabus_rate = (completed_topics / total_topics * 100.0) if total_topics else 0.0
        return Response({'success': True, 'stats': {
            'total_students_taught': students_count,
            'average_class_engagement': round(float(avg_eng), 1),
            'total_lectures_conducted': lectures_count,
            'syllabus_completion_rate': round(float(syllabus_rate), 1),
        }})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET', 'POST'])
@authentication_classes([])
@permission_classes([])
def api_timetable(request):
    try:
        teacher = _get_request_teacher(request)
        if request.method == 'POST':
            slot = Timetable.objects.create(
                teacher=teacher,
                subject=str(request.data.get('subject') or '').strip(),
                day_of_week=int(request.data.get('day_of_week') or 0),
                start_time=datetime.strptime(request.data.get('start_time'), '%H:%M').time(),
                end_time=datetime.strptime(request.data.get('end_time'), '%H:%M').time(),
                room_number=str(request.data.get('room_number') or '').strip(),
                is_active=bool(request.data.get('is_active', True)),
            )
            return Response({'success': True, 'slot': {
                'id': slot.id,
                'subject': slot.subject,
                'day_of_week': slot.day_of_week,
                'start_time': slot.start_time.strftime('%H:%M'),
                'end_time': slot.end_time.strftime('%H:%M'),
                'room_number': slot.room_number,
                'is_active': slot.is_active,
            }})

        rows = Timetable.objects.filter(teacher=teacher).order_by('day_of_week', 'start_time')
        return Response({'success': True, 'slots': [
            {
                'id': r.id,
                'subject': r.subject,
                'day_of_week': r.day_of_week,
                'day_label': r.get_day_of_week_display(),
                'start_time': r.start_time.strftime('%H:%M'),
                'end_time': r.end_time.strftime('%H:%M'),
                'room_number': r.room_number,
                'is_active': r.is_active,
            }
            for r in rows
        ]})
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['PUT', 'DELETE'])
@authentication_classes([])
@permission_classes([])
def api_timetable_detail(request, slot_id):
    try:
        teacher = _get_request_teacher(request)
        slot = Timetable.objects.get(id=slot_id, teacher=teacher)
        if request.method == 'DELETE':
            slot.delete()
            return Response({'success': True, 'message': 'Timetable slot removed'})
        for field in ['subject', 'room_number']:
            if field in request.data:
                setattr(slot, field, str(request.data.get(field) or '').strip())
        if 'day_of_week' in request.data:
            slot.day_of_week = int(request.data.get('day_of_week'))
        if 'start_time' in request.data and request.data.get('start_time'):
            slot.start_time = datetime.strptime(request.data.get('start_time'), '%H:%M').time()
        if 'end_time' in request.data and request.data.get('end_time'):
            slot.end_time = datetime.strptime(request.data.get('end_time'), '%H:%M').time()
        if 'is_active' in request.data:
            slot.is_active = bool(request.data.get('is_active'))
        slot.save()
        return Response({'success': True})
    except Timetable.DoesNotExist:
        return Response({'success': False, 'error': 'Timetable slot not found'}, status=404)
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)
