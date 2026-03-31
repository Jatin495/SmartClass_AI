"""
PCU Hackthon - Main URL Configuration
"""
from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView
from django.http import HttpResponse
import os


def serve_root_file(filename):
    """Serve a file directly from the project root directory"""
    def view(request):
        filepath = os.path.join(settings.BASE_DIR, filename)
        if not os.path.exists(filepath):
            return HttpResponse(f'File {filename} not found', status=404)
        with open(filepath, 'rb') as f:
            content = f.read()
        # Determine content type
        if filename.endswith('.css'):
            ct = 'text/css'
        elif filename.endswith('.js'):
            ct = 'application/javascript'
        elif filename.endswith('.html'):
            ct = 'text/html'
        elif filename.endswith('.png'):
            ct = 'image/png'
        elif filename.endswith('.jpg') or filename.endswith('.jpeg'):
            ct = 'image/jpeg'
        else:
            ct = 'application/octet-stream'
        return HttpResponse(content, content_type=ct)
    return view


urlpatterns = [
    path('admin/', admin.site.urls),

    # API endpoints
    path('api/', include('engagement.urls')),

    # Serve CSS/JS files from root directory
    path('styles.css', serve_root_file('styles.css'), name='styles_css'),
    path('main.js', serve_root_file('main.js'), name='main_js'),
    path('api.js', serve_root_file('api.js'), name='api_js'),
    path('charts.js', serve_root_file('charts.js'), name='charts_js'),

    # Serve HTML pages - both with .html extension AND without
    path('', serve_root_file('index.html'), name='home'),
    path('simple_live_camera.html', serve_root_file('simple_live_camera.html'), name='simple_live_camera_html'),
    path('index.html', serve_root_file('index.html'), name='home_html'),
    path('login.html', serve_root_file('login.html'), name='login_html'),
    path('dashboard.html', serve_root_file('dashboard.html'), name='dashboard_html'),
    path('teacher.html', serve_root_file('teacher.html'), name='teacher_html'),
    path('teacher_profile.html', serve_root_file('teacher_profile.html'), name='teacher_profile_html'),
    path('timetable.html', serve_root_file('timetable.html'), name='timetable_html'),
    path('teacher_dashboard.html', serve_root_file('teacher.html'), name='teacher_dashboard_html'),
    path('live_class.html', serve_root_file('live_class.html'), name='live_class_html'),
    path('attendance.html', serve_root_file('attendance.html'), name='attendance_html'),
    path('student.html', serve_root_file('student.html'), name='student_html'),
    path('reports.html', serve_root_file('reports.html'), name='reports_html'),
    path('analytics.html', serve_root_file('analytics.html'), name='analytics_html'),
    path('settings.html', serve_root_file('settings.html'), name='settings_html'),
    path('alert_test.html', serve_root_file('alert_test.html'), name='alert_test_html'),
    path('notification_center.html', serve_root_file('notification_center.html'), name='notification_center_html'),
    path('ai_insights.html', serve_root_file('ai_insights.html'), name='ai_insights_html'),

    # Face Recognition
    # path('register-face/', views.register_student_face, name='register_student_face'),
    # path('register-face/', views.register_student_face, name='register_student_face_html'),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
