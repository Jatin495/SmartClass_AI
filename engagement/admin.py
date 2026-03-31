"""
PCU Hackthon - Django Admin Configuration
"""
from django.contrib import admin
from .models import (
    Teacher,
    Student,
    ClassSession,
    Attendance,
    EngagementRecord,
    ClassEngagementSnapshot,
    Alert,
    Report,
    SyllabusTopic,
    DailyLectureTopic,
    StudentTopicProgress,
    ExtraLecturePlan,
    LectureFeedback,
)

@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'subject', 'is_active', 'created_at')
    search_fields = ('name', 'email')
    list_filter = ('is_active',)


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ('student_id', 'name', 'email', 'seat_row', 'seat_col', 'is_active', 'created_at')
    search_fields = ('student_id', 'name', 'email')
    list_filter = ('is_active',)


@admin.register(ClassSession)
class ClassSessionAdmin(admin.ModelAdmin):
    list_display = ('class_name', 'subject', 'teacher', 'start_time', 'status', 'duration_minutes')
    list_filter = ('status',)
    search_fields = ('class_name', 'subject')


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ('student', 'session', 'date', 'is_present', 'arrival_time')
    list_filter = ('is_present', 'date')
    search_fields = ('student__name',)


@admin.register(EngagementRecord)
class EngagementRecordAdmin(admin.ModelAdmin):
    list_display = ('student', 'session', 'timestamp', 'engagement_score', 'emotion', 'face_detected')
    list_filter = ('emotion', 'face_detected')
    search_fields = ('student__name',)
    ordering = ('-timestamp',)


@admin.register(ClassEngagementSnapshot)
class ClassEngagementSnapshotAdmin(admin.ModelAdmin):
    list_display = ('session', 'timestamp', 'avg_engagement', 'present_count', 'confusion_alert')
    list_filter = ('confusion_alert', 'low_engagement_alert')


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ('alert_type', 'severity', 'student', 'session', 'timestamp', 'is_resolved')
    list_filter = ('alert_type', 'severity', 'is_resolved')
    actions = ['mark_resolved']

    @admin.action(description="Mark selected alerts as resolved")
    def mark_resolved(self, request, queryset):
        from django.utils import timezone
        queryset.update(is_resolved=True, resolved_at=timezone.now())


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ('name', 'report_type', 'format', 'status', 'created_at', 'generated_at')
    list_filter = ('report_type', 'format', 'status')
    search_fields = ('name',)
    ordering = ('-created_at',)


@admin.register(SyllabusTopic)
class SyllabusTopicAdmin(admin.ModelAdmin):
    list_display = ('topic', 'subject', 'unit', 'teacher', 'status', 'checkpoint_assigned', 'checkpoint_completion_rate', 'is_delayed')
    list_filter = ('status', 'checkpoint_assigned', 'is_delayed', 'subject')
    search_fields = ('topic', 'subject', 'unit', 'teacher__name')
    ordering = ('subject', 'unit', 'topic')


@admin.register(DailyLectureTopic)
class DailyLectureTopicAdmin(admin.ModelAdmin):
    list_display = ('topic', 'lecture_date', 'is_completed', 'completed_at')
    list_filter = ('lecture_date', 'is_completed')
    search_fields = ('topic__topic', 'topic__subject', 'topic__unit')
    ordering = ('-lecture_date',)


@admin.register(StudentTopicProgress)
class StudentTopicProgressAdmin(admin.ModelAdmin):
    list_display = ('student', 'topic', 'completion_percent', 'needs_extra_lecture', 'updated_at')
    list_filter = ('needs_extra_lecture', 'topic__subject')
    search_fields = ('student__student_id', 'student__name', 'topic__topic')
    ordering = ('student__student_id', 'topic__topic')


@admin.register(ExtraLecturePlan)
class ExtraLecturePlanAdmin(admin.ModelAdmin):
    list_display = ('student', 'topic', 'scheduled_date', 'status', 'created_at')
    list_filter = ('status', 'scheduled_date', 'topic__subject')
    search_fields = ('student__student_id', 'student__name', 'topic__topic')
    ordering = ('-scheduled_date', '-created_at')


@admin.register(LectureFeedback)
class LectureFeedbackAdmin(admin.ModelAdmin):
    list_display = ('lecture_title', 'rating', 'submitted_at')
    list_filter = ('rating', 'submitted_at')
    search_fields = ('lecture_title', 'comment')
    ordering = ('-submitted_at',)
