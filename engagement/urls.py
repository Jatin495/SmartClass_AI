"""
PCU Hackthon - Engagement App URL Patterns
All /api/ routes handled here.
"""

from django.urls import path
from . import views
from . import camera

urlpatterns = [
    # Health check
    path('health/', views.api_health, name='api_health'),
    path('model-status/', views.model_status, name='model_status'),

    # Camera endpoints (using consolidated camera)
    path('simple_camera_feed/', camera.simple_camera_feed, name='simple_camera_feed'),
    path('start_simple_camera/', camera.start_simple_camera, name='start_simple_camera'),
    path('stop_simple_camera/', camera.stop_simple_camera, name='stop_simple_camera'),
    path('emotion_stats/', camera.get_emotion_stats, name='emotion_stats'),

    # Auth
    path('auth/login/', views.login, name='login'),
    path('auth/logout/', views.logout_view, name='logout'),

    # Dashboard
    path('dashboard/stats/', views.dashboard_stats, name='dashboard_stats'),
    path('dashboard/timeline/', views.engagement_timeline, name='engagement_timeline'),
    path('dashboard/heatmap/', views.classroom_heatmap, name='classroom_heatmap'),

    # Students
    path('students/', views.list_students, name='list_students'),
    path('students/overview/', views.students_overview, name='students_overview'),
    path('students/add/', views.add_student, name='add_student'),
    path('students/face-check/', views.face_capture_check, name='face_capture_check'),
    
    # Face Recognition
    # path('register-face/', views.register_student_face, name='register_student_face'),

    # Sessions
    path('sessions/', views.list_sessions, name='list_sessions'),
    path('sessions/start/', views.start_session, name='start_session'),
    path('sessions/<int:session_id>/end/', views.end_session, name='end_session'),
    path('sessions/<int:session_id>/report/', views.session_report, name='session_report'),

    # Live monitoring
    path('live/feed/', views.video_feed, name='video_feed'),
    path('live/data/', views.live_data, name='live_data'),
    path('live/frame/', views.stream_frame, name='stream_frame'),
    path('live/stop/', views.stop_stream_force, name='stop_stream_force'),

    # Alert System
    path('check-alert/', views.check_engagement_alert, name='check_engagement_alert'),
    path('create-test-data/', views.create_test_engagement_data, name='create_test_engagement_data'),
    path('alerts/', views.list_alerts, name='list_alerts'),
    path('alerts/<int:alert_id>/resolve/', views.resolve_alert, name='resolve_alert'),

    # Reports
    path('reports/generate/', views.generate_report, name='generate_report'),
    path('reports/list/', views.list_reports, name='list_reports'),
    path('reports/download/<str:report_id>/', views.download_report, name='download_report'),
    path('reports/delete/<str:report_id>/', views.delete_report, name='delete_report'),
    path('reports/templates/', views.report_templates, name='report_templates'),
    path('reports/schedule/', views.schedule_report, name='schedule_report'),

    # Attendance
    path('attendance/', views.attendance_report, name='attendance_report'),

    # Analytics
    path('analytics/', views.analytics_summary, name='analytics_summary'),

    # Setup / Demo
    path('setup/seed/', views.seed_demo_data, name='seed_demo_data'),

    # Database overview
    path('database/tables/', views.database_tables_overview, name='database_tables_overview'),

    # Teacher dashboard
    path('teacher/dashboard-data/', views.teacher_dashboard_data, name='teacher_dashboard_data'),
    path('teacher/syllabus/topics/add/', views.teacher_add_syllabus_topic, name='teacher_add_syllabus_topic'),
    path('teacher/syllabus/topics/<int:topic_id>/status/', views.teacher_update_topic_status, name='teacher_update_topic_status'),
    path('teacher/planner/add/', views.teacher_add_daily_topic, name='teacher_add_daily_topic'),
    path('teacher/planner/<int:plan_id>/complete/', views.teacher_complete_daily_topic, name='teacher_complete_daily_topic'),
    path('teacher/extra-lectures/schedule/', views.teacher_schedule_extra_lecture, name='teacher_schedule_extra_lecture'),
    path('teacher/feedback/add/', views.teacher_add_feedback, name='teacher_add_feedback'),

    # NEW: Notifications
    path('notifications/', views.api_notifications, name='api_notifications'),
    path('notifications/mark-read/', views.api_mark_notification_read, name='api_mark_notification_read'),

    # NEW: AI Insights
    path('ai-insights/', views.api_ai_insights, name='api_ai_insights'),
    path('ai-insights/<str:student_id>/', views.api_ai_insights_by_student, name='api_ai_insights_by_student'),

    # NEW: At-Risk Students
    path('students/at-risk/', views.api_students_at_risk, name='api_students_at_risk'),

    # NEW: Bulk Attendance
    path('attendance/bulk-mark/', views.api_attendance_bulk_mark, name='api_attendance_bulk_mark'),

    # NEW: Dashboard Summary
    path('dashboard/summary/', views.api_dashboard_summary, name='api_dashboard_summary'),

    # Teacher module v2 - syllabus
    path('syllabus/', views.api_syllabus, name='api_syllabus'),
    path('syllabus/<int:syllabus_id>/', views.api_syllabus_detail, name='api_syllabus_detail'),
    path('syllabus/progress/', views.api_syllabus_progress, name='api_syllabus_progress'),
    path('syllabus/delayed/', views.api_syllabus_delayed, name='api_syllabus_delayed'),
    path('syllabus/auto-heal/', views.api_syllabus_auto_heal, name='api_syllabus_auto_heal'),
    path('syllabus/auto-heal/accept/', views.api_syllabus_auto_heal_accept, name='api_syllabus_auto_heal_accept'),
    path('syllabus/<int:syllabus_id>/reschedule/', views.api_syllabus_reschedule, name='api_syllabus_reschedule'),

    # Lecture planner
    path('lecture-plan/', views.api_lecture_plan, name='api_lecture_plan'),
    path('lecture-plan/<int:plan_id>/', views.api_lecture_plan_detail, name='api_lecture_plan_detail'),
    path('lecture-plan/history/', views.api_lecture_plan_history, name='api_lecture_plan_history'),

    # Checkpoints
    path('checkpoints/', views.api_checkpoints, name='api_checkpoints'),
    path('checkpoints/<int:checkpoint_id>/', views.api_checkpoint_detail, name='api_checkpoint_detail'),
    path('checkpoints/<int:checkpoint_id>/results/', views.api_checkpoint_results, name='api_checkpoint_results'),
    path('checkpoints/send-reminder/', views.api_checkpoint_send_reminder, name='api_checkpoint_send_reminder'),
    path('checkpoints/summary/', views.api_checkpoint_summary, name='api_checkpoint_summary'),

    # Extra lectures and lagging students
    path('students/lagging/', views.api_students_lagging, name='api_students_lagging'),
    path('extra-lectures/', views.api_extra_lectures, name='api_extra_lectures'),
    path('extra-lectures/<int:lecture_id>/', views.api_extra_lecture_detail, name='api_extra_lecture_detail'),
    path('extra-lectures/send-note/', views.api_extra_lecture_send_note, name='api_extra_lecture_send_note'),

    # Performance reports
    path('students/performance/', views.api_students_performance, name='api_students_performance'),
    path('students/<int:student_pk>/performance/', views.api_student_performance_detail, name='api_student_performance_detail'),
    path('students/<int:student_pk>/engagement-trend/', views.api_student_engagement_trend, name='api_student_engagement_trend'),
    path('students/<int:student_pk>/attendance-calendar/', views.api_student_attendance_calendar, name='api_student_attendance_calendar'),
    path('students/<int:student_pk>/ai-recommendation/', views.api_student_ai_recommendation, name='api_student_ai_recommendation'),
    path('students/<str:student_id>/', views.student_detail, name='student_detail'),

    # Feedback v2
    path('feedback/', views.api_feedback_v2, name='api_feedback_v2'),
    path('feedback/<int:feedback_id>/reply/', views.api_feedback_reply, name='api_feedback_reply'),
    path('feedback/<int:feedback_id>/', views.api_feedback_delete, name='api_feedback_delete'),
    path('feedback/summary/', views.api_feedback_summary, name='api_feedback_summary'),
    path('feedback/export/', views.api_feedback_export, name='api_feedback_export'),

    # Teacher profile + activity
    path('teacher/profile/', views.api_teacher_profile, name='api_teacher_profile'),
    path('teacher/profile/photo/', views.api_teacher_profile_photo, name='api_teacher_profile_photo'),
    path('teacher/activity-log/', views.api_teacher_activity_log, name='api_teacher_activity_log'),
    path('teacher/stats/', views.api_teacher_stats, name='api_teacher_stats'),

    # Timetable
    path('timetable/', views.api_timetable, name='api_timetable'),
    path('timetable/<int:slot_id>/', views.api_timetable_detail, name='api_timetable_detail'),
]
