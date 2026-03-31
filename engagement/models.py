"""
PCU Hackthon - Database Models
Tables: Teacher, Student, Session, EngagementRecord, EmotionRecord, Alert, Attendance
"""

from django.db import models
from django.utils import timezone
from django.contrib.auth import get_user_model
import json

User = get_user_model()


class Teacher(models.Model):
    """Teacher/User accounts"""
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    password_hash = models.CharField(max_length=256)
    subject = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.email})"

    class Meta:
        db_table = 'teachers'


class Student(models.Model):
    """Student profiles"""
    RISK_LEVEL_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]
    
    student_id = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True, default='')
    seat_row = models.IntegerField(default=1)
    seat_col = models.IntegerField(default=1)
    face_encoding = models.TextField(blank=True, null=True)  # JSON encoded face embedding
    photo = models.ImageField(upload_to='students/', blank=True, null=True)
    profile_photo = models.ImageField(upload_to='students/profiles/', blank=True, null=True)
    risk_level = models.CharField(max_length=10, choices=RISK_LEVEL_CHOICES, default='low')
    created_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.student_id} - {self.name}"

    class Meta:
        db_table = 'students'


class ClassSession(models.Model):
    """Class sessions tracking"""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('ended', 'Ended'),
        ('paused', 'Paused'),
    ]
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='sessions')
    class_name = models.CharField(max_length=100)
    subject = models.CharField(max_length=100)
    start_time = models.DateTimeField(default=timezone.now)
    end_time = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    camera_source = models.CharField(max_length=50, default='0')
    total_students = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.class_name} - {self.start_time.strftime('%Y-%m-%d %H:%M')}"

    @property
    def duration_minutes(self):
        if self.end_time:
            delta = self.end_time - self.start_time
            return int(delta.total_seconds() / 60)
        elif self.status == 'active':
            delta = timezone.now() - self.start_time
            return int(delta.total_seconds() / 60)
        return 0

    class Meta:
        db_table = 'class_sessions'
        ordering = ['-start_time']


class Attendance(models.Model):
    """Daily attendance records"""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='attendance_records')
    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='attendance')
    date = models.DateField(default=timezone.now)
    is_present = models.BooleanField(default=False)
    arrival_time = models.DateTimeField(null=True, blank=True)
    departure_time = models.DateTimeField(null=True, blank=True)
    detection_confidence = models.FloatField(default=0.0)

    def __str__(self):
        status = "Present" if self.is_present else "Absent"
        return f"{self.student.name} - {self.date} - {status}"

    class Meta:
        db_table = 'attendance'
        unique_together = ['student', 'session', 'date']


class EngagementRecord(models.Model):
    """Per-student engagement scores recorded every 5 seconds"""
    EMOTION_CHOICES = [
        ('happy', 'Happy'),
        ('neutral', 'Neutral'),
        ('sad', 'Sad'),
        ('angry', 'Angry'),
        ('surprise', 'Surprise'),
        ('fear', 'Fear'),
        ('disgust', 'Disgust'),
        ('confused', 'Confused'),
        ('bored', 'Bored'),
        ('focused', 'Focused'),
        ('unknown', 'Unknown'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='engagement_records')
    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='engagement_records')
    timestamp = models.DateTimeField(default=timezone.now)
    
    # Engagement metrics
    engagement_score = models.FloatField(default=0.0)  # 0-100
    attention_score = models.FloatField(default=0.0)   # 0-100
    
    # Emotion detection
    emotion = models.CharField(max_length=20, choices=EMOTION_CHOICES, default='unknown')
    emotion_confidence = models.FloatField(default=0.0)
    emotion_scores = models.TextField(blank=True, default='{}')  # JSON: all emotion probabilities
    
    # Posture analysis (MediaPipe)
    head_angle = models.FloatField(default=0.0)        # Head tilt angle
    eye_contact = models.BooleanField(default=False)   # Looking at board/camera
    posture_score = models.FloatField(default=0.0)     # 0-100
    is_slouching = models.BooleanField(default=False)
    
    # Face detection data
    face_detected = models.BooleanField(default=False)
    face_confidence = models.FloatField(default=0.0)
    face_bbox = models.CharField(max_length=100, blank=True, default='')  # x,y,w,h
    
    # Frame reference
    frame_path = models.CharField(max_length=255, blank=True, default='')
    engagement_trend = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.student.name} - {self.timestamp} - Eng:{self.engagement_score:.0f}%"

    def get_emotion_scores(self):
        try:
            return json.loads(self.emotion_scores)
        except:
            return {}
    
    def get_engagement_trend(self):
        try:
            if isinstance(self.engagement_trend, dict):
                return self.engagement_trend
            return json.loads(self.engagement_trend)
        except:
            return {}

    class Meta:
        db_table = 'engagement_records'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['student', 'session']),
            models.Index(fields=['timestamp']),
        ]


class ClassEngagementSnapshot(models.Model):
    """Class-wide engagement snapshot every 5 seconds"""
    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='snapshots')
    timestamp = models.DateTimeField(default=timezone.now)
    
    # Aggregate metrics
    avg_engagement = models.FloatField(default=0.0)
    avg_attention = models.FloatField(default=0.0)
    present_count = models.IntegerField(default=0)
    
    # Emotion distribution (JSON)
    emotion_distribution = models.TextField(default='{}')
    
    # Alert flags
    confusion_alert = models.BooleanField(default=False)  # >30% confused
    low_engagement_alert = models.BooleanField(default=False)

    def get_emotion_distribution(self):
        try:
            return json.loads(self.emotion_distribution)
        except:
            return {}

    class Meta:
        db_table = 'class_snapshots'
        ordering = ['-timestamp']


class Alert(models.Model):
    """Alerts for teacher attention"""
    ALERT_TYPES = [
        ('low_engagement', 'Low Engagement'),
        ('confused', 'Confused'),
        ('distracted', 'Distracted'),
        ('absent', 'Absent'),
        ('class_confusion', 'Class Confusion'),
        ('bored', 'Bored'),
    ]
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]

    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='alerts')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='alerts', null=True, blank=True)
    alert_type = models.CharField(max_length=30, choices=ALERT_TYPES)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='medium')
    message = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now)
    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        student_name = self.student.name if self.student else "Class"
        return f"{self.alert_type} - {student_name} - {self.timestamp}"

    class Meta:
        db_table = 'alerts'
        ordering = ['-timestamp']


class Report(models.Model):
    """Generated reports for analytics and insights"""
    REPORT_TYPES = [
        ('engagement', 'Engagement Report'),
        ('attendance', 'Attendance Report'),
        ('performance', 'Performance Report'),
        ('emotion', 'Emotion Analysis'),
        ('summary', 'Summary Report'),
    ]
    
    FORMATS = [
        ('csv', 'CSV'),
        ('pdf', 'PDF'),
        ('xlsx', 'Excel'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('generating', 'Generating'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    name = models.CharField(max_length=200)
    report_type = models.CharField(max_length=20, choices=REPORT_TYPES)
    format = models.CharField(max_length=5, choices=FORMATS, default='csv')
    file_path = models.CharField(max_length=500, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)  # in bytes
    
    # Report parameters
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    student_ids = models.TextField(null=True, blank=True)  # JSON string of student IDs
    
    def __str__(self):
        return f"{self.name} - {self.get_report_type_display()}"
    
    class Meta:
        db_table = 'reports'
        ordering = ['-created_at']


class SyllabusTopic(models.Model):
    """Teacher-defined syllabus topics and completion state."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in-progress', 'In Progress'),
        ('completed', 'Completed'),
    ]

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='syllabus_topics')
    subject = models.CharField(max_length=120)
    unit = models.CharField(max_length=120)
    topic = models.CharField(max_length=180)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    planned_date = models.DateField(default=timezone.now)
    revised_date = models.DateField(null=True, blank=True)
    is_delayed = models.BooleanField(default=False)
    checkpoint_assigned = models.BooleanField(default=False)
    checkpoint_completion_rate = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.subject} - {self.unit} - {self.topic}"

    class Meta:
        db_table = 'syllabus_topics'
        ordering = ['created_at']


class DailyLectureTopic(models.Model):
    """Topics planned for a specific lecture day."""
    topic = models.ForeignKey(SyllabusTopic, on_delete=models.CASCADE, related_name='daily_plans')
    lecture_date = models.DateField(default=timezone.now)
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.topic.topic} ({self.lecture_date})"

    class Meta:
        db_table = 'daily_lecture_topics'
        unique_together = ['topic', 'lecture_date']
        ordering = ['-lecture_date', 'topic__topic']


class StudentTopicProgress(models.Model):
    """Per-student progress in each syllabus topic."""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='topic_progress')
    topic = models.ForeignKey(SyllabusTopic, on_delete=models.CASCADE, related_name='student_progress')
    completion_percent = models.FloatField(default=0.0)
    needs_extra_lecture = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.student.student_id} - {self.topic.topic} ({self.completion_percent:.0f}%)"

    class Meta:
        db_table = 'student_topic_progress'
        unique_together = ['student', 'topic']


class ExtraLecturePlan(models.Model):
    """Extra support sessions for lagging students/topics."""
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('completed', 'Completed'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='extra_lectures')
    topic = models.ForeignKey(SyllabusTopic, on_delete=models.CASCADE, related_name='extra_lectures')
    scheduled_date = models.DateField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.student_id} - {self.topic.topic} ({self.scheduled_date})"

    class Meta:
        db_table = 'extra_lecture_plans'
        ordering = ['-scheduled_date', '-created_at']


class LectureFeedback(models.Model):
    """Student feedback collected after lectures."""
    lecture_title = models.CharField(max_length=200)
    rating = models.FloatField(default=0.0)
    comment = models.TextField(blank=True, default='')
    submitted_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.lecture_title} ({self.rating:.1f})"

    class Meta:
        db_table = 'lecture_feedback'
        ordering = ['-submitted_at']


class Notification(models.Model):
    """System and engagement notifications for teachers"""
    NOTIFICATION_TYPES = [
        ('alert', 'Alert'),
        ('system', 'System'),
        ('info', 'Info'),
    ]
    
    type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES, default='alert')
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    related_student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='notifications', null=True, blank=True)
    
    def __str__(self):
        student_ref = f" - {self.related_student.name}" if self.related_student else ""
        return f"{self.type}: {self.message[:50]}{student_ref}"
    
    class Meta:
        db_table = 'notifications'
        ordering = ['-created_at']


class AIInsight(models.Model):
    """AI-generated insights and recommendations per student"""
    RISK_LEVEL_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]
    
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='ai_insights')
    week_start_date = models.DateField()
    engagement_trend = models.JSONField(default=dict, blank=True)
    risk_level = models.CharField(max_length=10, choices=RISK_LEVEL_CHOICES, default='low')
    recommendation_text = models.TextField(blank=True, default='')
    generated_at = models.DateTimeField(default=timezone.now)
    
    def get_engagement_trend(self):
        try:
            if isinstance(self.engagement_trend, dict):
                return self.engagement_trend
            return json.loads(self.engagement_trend)
        except:
            return {}
    
    def __str__(self):
        return f"Insights: {self.student.student_id} ({self.week_start_date.strftime('%Y-%m-%d')}) - Risk: {self.risk_level}"
    
    class Meta:
        db_table = 'ai_insights'
        ordering = ['-week_start_date']
        unique_together = ['student', 'week_start_date']


class Syllabus(models.Model):
    """Detailed syllabus planning model for teacher workflow."""
    PRIORITY_CHOICES = [
        ('high', 'High'),
        ('medium', 'Medium'),
        ('low', 'Low'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
    ]

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='syllabus_items')
    subject = models.CharField(max_length=120)
    unit = models.CharField(max_length=120)
    topic = models.CharField(max_length=220)
    estimated_hours = models.FloatField(default=1.0)
    target_date = models.DateField()
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    auto_healing_date = models.DateField(null=True, blank=True)
    is_auto_healed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.subject} / {self.unit} / {self.topic}"

    class Meta:
        db_table = 'syllabus'
        ordering = ['target_date', 'created_at']


class LecturePlan(models.Model):
    """Day-level lecture planning linked to syllabus items."""
    STATUS_CHOICES = [
        ('planned', 'Planned'),
        ('done', 'Done'),
        ('skipped', 'Skipped'),
        ('in_progress', 'In Progress'),
    ]

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='lecture_plans')
    topic = models.ForeignKey(Syllabus, on_delete=models.CASCADE, related_name='lecture_plans')
    lecture_date = models.DateField(default=timezone.now)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='planned')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.topic.topic} ({self.lecture_date})"

    class Meta:
        db_table = 'lecture_plan'
        ordering = ['-lecture_date', 'start_time', '-created_at']


class Checkpoint(models.Model):
    """Checkpoint/quiz metadata per syllabus topic."""
    TYPE_CHOICES = [
        ('mcq', 'MCQ'),
        ('truefalse', 'True-False'),
        ('shortanswer', 'Short Answer'),
    ]

    topic = models.ForeignKey(Syllabus, on_delete=models.CASCADE, related_name='checkpoints')
    title = models.CharField(max_length=200)
    checkpoint_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    passing_score = models.IntegerField(default=60)
    deadline = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({self.topic.topic})"

    class Meta:
        db_table = 'checkpoint'
        ordering = ['-created_at']


class CheckpointResult(models.Model):
    """Per-student checkpoint result."""
    checkpoint = models.ForeignKey(Checkpoint, on_delete=models.CASCADE, related_name='results')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='checkpoint_results')
    score = models.FloatField(default=0.0)
    attempted_at = models.DateTimeField(default=timezone.now)
    passed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.student.student_id} - {self.checkpoint.title} ({self.score})"

    class Meta:
        db_table = 'checkpoint_result'
        ordering = ['-attempted_at']
        unique_together = ['checkpoint', 'student']


class ExtraLecture(models.Model):
    """Teacher scheduled extra lectures for specific students/topics."""
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ]

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='extra_lectures_v2')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='extra_lectures_v2')
    topic = models.ForeignKey(Syllabus, on_delete=models.CASCADE, related_name='extra_lectures_v2')
    scheduled_date = models.DateField()
    scheduled_time = models.TimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.student.name} - {self.topic.topic} ({self.scheduled_date})"

    class Meta:
        db_table = 'extra_lecture'
        ordering = ['-scheduled_date', '-created_at']


class Feedback(models.Model):
    """Lecture feedback with anonymity and teacher reply support."""
    lecture = models.ForeignKey(LecturePlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='feedback_entries')
    student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True, related_name='feedback_entries')
    rating = models.IntegerField(default=5)
    comment = models.TextField(blank=True, default='')
    is_anonymous = models.BooleanField(default=False)
    teacher_reply = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        lecture_name = self.lecture.topic.topic if self.lecture and self.lecture.topic else 'General'
        return f"Feedback {self.rating}* - {lecture_name}"

    class Meta:
        db_table = 'feedback'
        ordering = ['-created_at']


class TeacherProfile(models.Model):
    """Teacher profile details page model."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='teacher_profile')
    teacher = models.OneToOneField(Teacher, on_delete=models.CASCADE, related_name='profile')
    department = models.CharField(max_length=120, blank=True, default='')
    employee_id = models.CharField(max_length=50, blank=True, default='')
    phone = models.CharField(max_length=20, blank=True, default='')
    profile_photo = models.ImageField(upload_to='teachers/', null=True, blank=True)
    subjects = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"Profile - {self.teacher.name}"

    class Meta:
        db_table = 'teacher_profile'


class ActivityLog(models.Model):
    """Teacher action history for activity feed."""
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='activity_logs')
    action_text = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.teacher.name}: {self.action_text}"

    class Meta:
        db_table = 'activity_log'
        ordering = ['-created_at']


class Timetable(models.Model):
    """Weekly class timetable per teacher."""
    DAYS = [
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    ]

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='timetable_slots')
    subject = models.CharField(max_length=120)
    day_of_week = models.IntegerField(choices=DAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    room_number = models.CharField(max_length=30)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.get_day_of_week_display()} {self.subject} ({self.start_time}-{self.end_time})"

    class Meta:
        db_table = 'timetable'
        ordering = ['day_of_week', 'start_time']
