// PCU Hackthon - Main JavaScript File



// Global state

let currentUser = null;

let classroomData = null;

let isMonitoring = false;

let selectedStudent = null;



// Initialize app

document.addEventListener('DOMContentLoaded', function() {

    initializeApp();

});



function initializeApp() {

    // Check if user is logged in

    const loggedInUser = localStorage.getItem('currentUser');

    if (loggedInUser) {

        currentUser = JSON.parse(loggedInUser);

        updateUIForLoggedInUser();

    }



    // Initialize navigation

    initializeNavigation();

    

    // Initialize modals

    initializeModals();

    

    // Initialize forms

    initializeForms();
    initializeFloatingQuickAction();
    initializeNotificationBadges();

    

    const isDashboardPage = (window.location.pathname || '').toLowerCase().includes('dashboard.html');

    // Keep dashboard fully backend-driven; other pages can still use mock fallback.
    if (!isDashboardPage) {
        // Load mock data
        loadMockData();

        // Initialize real-time updates
        initializeRealTimeUpdates();
    }

}

function initializeNotificationBadges() {
    const badges = document.querySelectorAll('.notification-badge');
    if (!badges || !badges.length) {
        return;
    }

    const refreshUnread = async () => {
        try {
            const response = await fetch('/api/notifications/');
            const data = await response.json();
            const unread = Number(data.unread || 0);
            badges.forEach((badge) => {
                badge.textContent = String(unread);
                badge.style.display = unread > 0 ? 'inline-flex' : 'none';
            });
        } catch (e) {
            // Keep existing badge values if API is temporarily unavailable.
        }
    };

    refreshUnread();
    setInterval(refreshUnread, 30000);
}

function initializeFloatingQuickAction() {
    const page = (window.location.pathname || '').toLowerCase();
    if (page.includes('login.html') || page.endsWith('/')) {
        return;
    }

    if (document.querySelector('.floating-quick-action')) {
        return;
    }

    const wrapper = document.createElement('div');
    wrapper.className = 'floating-quick-action';
    wrapper.innerHTML = `
        <div class="quick-action-menu">
            <a class="quick-action-item" href="live_class.html">Start Live Session</a>
            <a class="quick-action-item" href="attendance.html">Mark Attendance</a>
            <a class="quick-action-item" href="student.html">Add Student</a>
            <a class="quick-action-item" href="reports.html">Generate Report</a>
        </div>
        <button class="quick-action-main" aria-label="Quick Actions">+</button>
    `;

    const btn = wrapper.querySelector('.quick-action-main');
    btn.addEventListener('click', () => wrapper.classList.toggle('open'));

    document.body.appendChild(wrapper);
}



// Navigation

function initializeNavigation() {

    // Mobile menu toggle

    const menuToggle = document.getElementById('menuToggle');

    const sidebar = document.querySelector('.sidebar');

    

    if (menuToggle) {

        menuToggle.addEventListener('click', function() {

            sidebar.classList.toggle('open');

        });

    }



    // Set active nav item

    const currentPath = window.location.pathname;

    const navItems = document.querySelectorAll('.nav-item');

    

    navItems.forEach(item => {

        item.classList.remove('active');

        if (item.getAttribute('href') === currentPath) {

            item.classList.add('active');

        }

    });



    // Handle navigation clicks

    navItems.forEach(item => {

        item.addEventListener('click', function(e) {

            if (!this.getAttribute('href').startsWith('http')) {

                e.preventDefault();

                const href = this.getAttribute('href');

                if (href && href !== '#') {

                    window.location.href = href;

                }

            }

        });

    });

}



// Forms

function initializeForms() {

    // Login form

    const loginForm = document.getElementById('loginForm');

    if (loginForm) {

        loginForm.addEventListener('submit', handleLogin);

    }



    // Settings form

    const settingsForm = document.getElementById('settingsForm');

    if (settingsForm) {

        settingsForm.addEventListener('submit', handleSettings);

    }



    // Student search

    const studentSearch = document.getElementById('studentSearch');

    if (studentSearch) {

        studentSearch.addEventListener('input', handleStudentSearch);

    }

}



function handleLogin(e) {

    e.preventDefault();

    

    const email = document.getElementById('email').value;

    const password = document.getElementById('password').value;

    

    // Mock authentication

    if (email && password) {

        currentUser = {

            id: 'teacher001',

            name: 'Sarah Johnson',

            email: email,

            role: 'teacher'

        };

        

        localStorage.setItem('currentUser', JSON.stringify(currentUser));

        

        // Redirect to dashboard

        window.location.href = 'dashboard.html';

    } else {

        showAlert('Please enter both email and password', 'error');

    }

}



function handleSettings(e) {

    e.preventDefault();

    

    const formData = new FormData(e.target);

    const settings = {};

    

    for (let [key, value] of formData.entries()) {

        settings[key] = value;

    }

    

    // Save settings

    localStorage.setItem('settings', JSON.stringify(settings));

    showAlert('Settings saved successfully!', 'success');

}



function handleStudentSearch(e) {

    const searchTerm = e.target.value.toLowerCase();

    const studentCards = document.querySelectorAll('.student-card');

    

    studentCards.forEach(card => {

        const name = card.querySelector('.student-name').textContent.toLowerCase();

        const id = card.querySelector('.student-id').textContent.toLowerCase();

        

        if (name.includes(searchTerm) || id.includes(searchTerm)) {

            card.style.display = 'block';

        } else {

            card.style.display = 'none';

        }

    });

}



// Modals

function initializeModals() {

    // Close modal on outside click

    document.addEventListener('click', function(e) {

        if (e.target.classList.contains('modal')) {

            closeModal(e.target.id);

        }

    });



    // Close modal on X button

    document.querySelectorAll('.modal-close').forEach(btn => {

        btn.addEventListener('click', function() {

            const modal = this.closest('.modal');

            closeModal(modal.id);

        });

    });

}



function openModal(modalId) {

    const modal = document.getElementById(modalId);

    if (modal) {

        modal.classList.add('active');

        document.body.style.overflow = 'hidden';

    }

}



function closeModal(modalId) {

    const modal = document.getElementById(modalId);

    if (modal) {

        modal.classList.remove('active');

        document.body.style.overflow = '';

    }

}



// Mock Data

function loadMockData() {

    // Generate mock classroom data

    classroomData = {

        classId: 'CS101',

        className: 'Computer Science 101',

        totalStudents: 24,

        presentStudents: 22,

        averageEngagement: 78,

        emotions: {

            happy: 45,

            neutral: 30,

            confused: 15,

            bored: 10

        },

        students: generateMockStudents(),

        alerts: generateMockAlerts(),

        attendance: generateMockAttendance()

    };



    // Update UI with data

    updateDashboard();

    updateStudentCards();

    updateCharts();

}



function generateMockStudents() {

    const firstNames = ['Emma', 'Liam', 'Olivia', 'Noah', 'Ava', 'Ethan', 'Sophia', 'Mason', 'Isabella', 'William', 'Mia', 'James', 'Charlotte', 'Benjamin', 'Amelia', 'Lucas', 'Harper', 'Henry', 'Evelyn', 'Alexander'];

    const lastNames = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez', 'Wilson', 'Anderson', 'Taylor', 'Thomas', 'Moore', 'Jackson', 'Martin', 'Lee', 'Perez', 'Thompson'];

    

    const students = [];

    for (let i = 1; i <= 24; i++) {

        const firstName = firstNames[Math.floor(Math.random() * firstNames.length)];

        const lastName = lastNames[Math.floor(Math.random() * lastNames.length)];

        

        students.push({

            id: `STU${String(i).padStart(3, '0')}`,

            name: `${firstName} ${lastName}`,

            engagement: Math.floor(Math.random() * 40) + 60,

            emotion: ['happy', 'neutral', 'confused', 'bored'][Math.floor(Math.random() * 4)],

            present: Math.random() > 0.1,

            attention: Math.floor(Math.random() * 30) + 70

        });

    }

    

    return students;

}



function generateMockAlerts() {

    const alertTypes = ['low_engagement', 'distracted', 'confused', 'absent'];

    const alerts = [];

    

    for (let i = 0; i < 5; i++) {

        const type = alertTypes[Math.floor(Math.random() * alertTypes.length)];

        const student = classroomData.students[Math.floor(Math.random() * classroomData.students.length)];

        

        alerts.push({

            id: `ALT${String(i + 1).padStart(3, '0')}`,

            type: type,

            studentId: student.id,

            studentName: student.name,

            message: getAlertMessage(type, student.name),

            timestamp: new Date(Date.now() - Math.random() * 3600000).toISOString(),

            severity: type === 'absent' ? 'high' : 'medium'

        });

    }

    

    return alerts.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));

}



function getAlertMessage(type, studentName) {

    const messages = {

        low_engagement: `${studentName} shows low engagement`,

        distracted: `${studentName} appears distracted`,

        confused: `${studentName} seems confused`,

        absent: `${studentName} is absent`

    };

    return messages[type] || `${studentName} needs attention`;

}



function generateMockAttendance() {

    const attendance = {};

    const today = new Date().toISOString().split('T')[0];

    

    attendance[today] = classroomData.students.map(student => ({

        studentId: student.id,

        studentName: student.name,

        present: student.present,

        arrivalTime: student.present ? new Date(Date.now() - Math.random() * 3600000).toISOString() : null,

        departureTime: null

    }));

    

    return attendance;

}



// UI Updates

function updateUIForLoggedInUser() {

    const userAvatar = document.getElementById('userAvatar');

    const userName = document.getElementById('userName');

    

    if (userAvatar && userName && currentUser) {

        userAvatar.textContent = currentUser.name.split(' ').map(n => n[0]).join('').toUpperCase();

        userName.textContent = currentUser.name;

    }

}



function updateDashboard() {

    if (!classroomData) return;

    

    // Update stats

    updateStat('totalStudents', classroomData.totalStudents);

    updateStat('presentStudents', classroomData.presentStudents);

    updateStat('averageEngagement', classroomData.averageEngagement + '%');

    

    // Update alerts

    updateAlerts();

    

    // Update heatmap

    updateHeatmap();

}



function updateStat(id, value) {

    const element = document.getElementById(id);

    if (element) {

        element.textContent = value;

    }

}



function updateAlerts() {

    const alertsContainer = document.getElementById('alertsContainer');

    if (!alertsContainer || !classroomData) return;

    

    alertsContainer.innerHTML = '';

    

    classroomData.alerts.slice(0, 5).forEach(alert => {

        const alertElement = createAlertElement(alert);

        alertsContainer.appendChild(alertElement);

    });

}



function createAlertElement(alert) {

    const div = document.createElement('div');

    div.className = `alert alert-${alert.severity === 'high' ? 'danger' : 'warning'} fade-in`;

    

    const icon = alert.severity === 'high' ? '⚠️' : '⚡';

    const time = new Date(alert.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });

    

    div.innerHTML = `

        <span>${icon}</span>

        <div>

            <strong>${alert.studentName}</strong>

            <p>${alert.message}</p>

            <small>${time}</small>

        </div>

    `;

    

    return div;

}



function updateStudentCards() {

    const container = document.getElementById('studentCardsContainer');

    if (!container || !classroomData) return;

    

    container.innerHTML = '';

    

    classroomData.students.slice(0, 8).forEach(student => {

        const card = createStudentCard(student);

        container.appendChild(card);

    });

}



function createStudentCard(student) {

    const div = document.createElement('div');

    div.className = 'student-card fade-in';

    div.onclick = () => showStudentDetails(student.id);

    

    const engagementColor = student.engagement >= 80 ? 'success' : student.engagement >= 60 ? 'warning' : 'danger';

    

    div.innerHTML = `

        <div class="student-avatar">${student.name.split(' ').map(n => n[0]).join('')}</div>

        <div class="student-name">${student.name}</div>

        <div class="student-id">${student.id}</div>

        <div class="engagement-score">

            <div class="engagement-bar">

                <div class="engagement-fill" style="width: ${student.engagement}%"></div>

            </div>

            <span class="engagement-text">${student.engagement}%</span>

        </div>

        <span class="badge badge-${engagementColor}">${student.emotion}</span>

    `;

    

    return div;

}



function updateHeatmap() {

    const container = document.getElementById('heatmapContainer');

    if (!container || !classroomData) return;

    

    container.innerHTML = '';

    

    // Create 8x6 grid (48 seats for 24 students, 2 per seat)

    for (let row = 0; row < 6; row++) {

        for (let col = 0; col < 8; col++) {

            const seatIndex = row * 8 + col;

            const seat = document.createElement('div');

            seat.className = 'heatmap-seat';

            

            if (seatIndex < classroomData.students.length) {

                const student = classroomData.students[seatIndex];

                if (student.present) {

                    const engagementLevel = student.engagement >= 80 ? 'high' : student.engagement >= 60 ? 'medium' : 'low';

                    seat.classList.add(`heatmap-${engagementLevel}`);

                    seat.textContent = student.id.split('U')[1];

                    seat.title = `${student.name} - ${student.engagement}% engagement`;

                } else {

                    seat.classList.add('heatmap-empty');

                    seat.textContent = '-';

                }

            } else {

                seat.classList.add('heatmap-empty');

                seat.textContent = '';

            }

            

            container.appendChild(seat);

        }

    }

}



// Student Details

function showStudentDetails(studentId) {

    selectedStudent = classroomData.students.find(s => s.id === studentId);

    if (!selectedStudent) return;

    

    // Update modal content

    const modalTitle = document.getElementById('studentModalTitle');

    const modalContent = document.getElementById('studentModalContent');

    

    if (modalTitle) {

        modalTitle.textContent = selectedStudent.name;

    }

    

    if (modalContent) {

        modalContent.innerHTML = `

            <div class="student-details">

                <div class="detail-row">

                    <strong>Student ID:</strong> ${selectedStudent.id}

                </div>

                <div class="detail-row">

                    <strong>Current Engagement:</strong> ${selectedStudent.engagement}%

                </div>

                <div class="detail-row">

                    <strong>Current Emotion:</strong> ${selectedStudent.emotion}

                </div>

                <div class="detail-row">

                    <strong>Attention Level:</strong> ${selectedStudent.attention}%

                </div>

                <div class="detail-row">

                    <strong>Status:</strong> ${selectedStudent.present ? 'Present' : 'Absent'}

                </div>

                <canvas id="studentEngagementChart" width="400" height="200"></canvas>

            </div>

        `;

    }

    

    openModal('studentModal');

    

    // Draw student chart

    setTimeout(() => drawStudentEngagementChart(), 100);

}



// Live Class Monitoring

function startMonitoring() {

    isMonitoring = true;

    const btn = document.getElementById('monitoringBtn');

    if (btn) {

        btn.textContent = 'Stop Monitoring';

        btn.classList.remove('btn-success');

        btn.classList.add('btn-danger');

    }

    

    // Start real-time updates

    startRealTimeUpdates();

}



function stopMonitoring() {

    isMonitoring = false;

    const btn = document.getElementById('monitoringBtn');

    if (btn) {

        btn.textContent = 'Start Monitoring';

        btn.classList.remove('btn-danger');

        btn.classList.add('btn-success');

    }

    

    // Stop real-time updates

    stopRealTimeUpdates();

}



function toggleMonitoring() {

    if (isMonitoring) {

        stopMonitoring();

    } else {

        startMonitoring();

    }

}



// Real-time Updates

function initializeRealTimeUpdates() {

    // Update every 5 seconds

    setInterval(updateRealTimeData, 5000);

}



function startRealTimeUpdates() {

    // Update every 2 seconds when monitoring

    realTimeInterval = setInterval(updateRealTimeData, 2000);

}



function stopRealTimeUpdates() {

    if (realTimeInterval) {

        clearInterval(realTimeInterval);

        realTimeInterval = null;

    }

}



function updateRealTimeData() {

    if (!classroomData) return;

    

    // Simulate real-time data changes

    classroomData.students.forEach(student => {

        if (student.present) {

            // Randomly fluctuate engagement

            const change = (Math.random() - 0.5) * 10;

            student.engagement = Math.max(0, Math.min(100, student.engagement + change));

            

            // Randomly change emotion

            if (Math.random() < 0.1) {

                const emotions = ['happy', 'neutral', 'confused', 'bored'];

                student.emotion = emotions[Math.floor(Math.random() * emotions.length)];

            }

        }

    });

    

    // Recalculate average

    const presentStudents = classroomData.students.filter(s => s.present);

    classroomData.averageEngagement = Math.round(

        presentStudents.reduce((sum, s) => sum + s.engagement, 0) / presentStudents.length

    );

    

    // Update UI

    updateDashboard();

    updateStudentCards();

    updateCharts();

}



// Charts

function updateCharts() {

    // This will be implemented in charts.js

    if (typeof updateEmotionChart === 'function') {

        updateEmotionChart();

    }

    if (typeof updateEngagementChart === 'function') {

        updateEngagementChart();

    }

}



// Utility Functions

function showAlert(message, type = 'info') {

    // Create alert element

    const alert = document.createElement('div');

    alert.className = `alert alert-${type} fade-in`;

    alert.style.position = 'fixed';

    alert.style.top = '20px';

    alert.style.right = '20px';

    alert.style.zIndex = '3000';

    alert.style.maxWidth = '400px';

    

    alert.innerHTML = `

        <span>${type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️'}</span>

        <span>${message}</span>

    `;

    

    document.body.appendChild(alert);

    

    // Auto remove after 3 seconds

    setTimeout(() => {

        alert.remove();

    }, 3000);

}



function formatTime(date) {

    return new Date(date).toLocaleTimeString('en-US', {

        hour: '2-digit',

        minute: '2-digit',

        hour12: true

    });

}



function formatDate(date) {

    return new Date(date).toLocaleDateString('en-US', {

        weekday: 'short',

        month: 'short',

        day: 'numeric'

    });

}



function downloadPDF() {

    // Mock PDF download

    showAlert('Generating PDF report...', 'info');

    

    setTimeout(() => {

        showAlert('PDF report downloaded successfully!', 'success');

    }, 2000);

}



function logout() {

    localStorage.removeItem('currentUser');

    currentUser = null;

    window.location.href = 'login.html';

}



// Export functions for use in other scripts

window.SmartClassMonitor = Object.assign(window.SmartClassMonitor || {}, {

    currentUser,

    classroomData,

    isMonitoring,

    selectedStudent,

    startMonitoring,

    stopMonitoring,

    toggleMonitoring,

    showStudentDetails,

    openModal,

    closeModal,

    showAlert,

    downloadPDF,

    logout

});

