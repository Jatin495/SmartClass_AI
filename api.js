/**
 * PCU Hackthon - API Bridge
 * Connects frontend HTML/JS to Django REST backend.
 * All backend calls go through this module.
 */

function resolveBackendOrigin() {
    const protocol = window.location.protocol;

    // When opened directly via file://, window.location.origin is not usable for API calls.
    if (protocol === 'file:') {
        const saved = localStorage.getItem('backendOrigin');
        return saved || 'http://127.0.0.1:8000';
    }

    return window.location.origin;
}

const ORIGIN = resolveBackendOrigin();
const API_BASE = `${ORIGIN}/api`;
const MJPEG_URL = `${ORIGIN}/api/live/feed/`;

// ─── API Client ───────────────────────────────────────────────────────────────
const API = {
    async get(endpoint, params = {}) {
        const url = new URL(`${API_BASE}${endpoint}`);
        Object.keys(params).forEach(k => url.searchParams.append(k, params[k]));
        try {
            const res = await fetch(url, {
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                credentials: 'include',
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return await res.json();
        } catch (e) {
            console.warn(`[API] GET ${endpoint} failed:`, e.message);
            return null;
        }
    },

    async post(endpoint, data = {}) {
        try {
            const res = await fetch(`${API_BASE}${endpoint}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                credentials: 'include',
                body: JSON.stringify(data),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return await res.json();
        } catch (e) {
            console.warn(`[API] POST ${endpoint} failed:`, e.message);
            return null;
        }
    },

    async delete(endpoint) {
        try {
            const res = await fetch(`${API_BASE}${endpoint}`, {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                credentials: 'include',
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return await res.json();
        } catch (e) {
            console.warn(`[API] DELETE ${endpoint} failed:`, e.message);
            return null;
        }
    }
};

// ─── Backend Connection Status ────────────────────────────────────────────────
let backendConnected = false;
let connectionCheckInterval = null;

async function checkBackendConnection() {
    let result = await API.get('/health/');

    // Retry once after a short delay to avoid false offline during server warm-up.
    if (result === null) {
        await new Promise(resolve => setTimeout(resolve, 800));
        result = await API.get('/health/');
    }

    backendConnected = result !== null;
    updateConnectionUI(backendConnected, result);
    return backendConnected;
}

function updateConnectionUI(connected, data) {
    const indicator = document.getElementById('backendStatus');
    if (indicator) {
        indicator.textContent = connected ? '🟢 Backend Connected' : '🔴 Backend Offline (Demo Mode)';
        indicator.style.color = connected ? '#22c55e' : '#ef4444';
    }

    // If connected, update student count etc.
    if (connected && data) {
        console.log(`✅ Backend connected | Students: ${data.students} | Sessions: ${data.sessions}`);
    } else {
        console.warn('⚠️ Backend not available. Running in demo mode.');
    }
}

// ─── Dashboard Integration ────────────────────────────────────────────────────
async function loadDashboardFromBackend() {
    if (!backendConnected) return false;

    // Load stats
    const stats = await API.get('/dashboard/stats/');
    if (stats) {
        updateStat('totalStudents', stats.total_students || '-');
        updateStat('presentStudents', stats.present_today || '-');
        updateStat('averageEngagement', (stats.avg_engagement || 0) + '%');
    }

    // Load heatmap
    const heatmapData = await API.get('/dashboard/heatmap/');
    if (heatmapData && heatmapData.heatmap) {
        renderBackendHeatmap(heatmapData.heatmap);
    }

    // Load timeline
    const timeline = await API.get('/dashboard/timeline/');
    if (timeline && window.ChartUtils) {
        // Trigger chart update with backend data
        window._backendTimeline = timeline.timeline;
    }

    return true;
}

function renderBackendHeatmap(heatmapData) {
    const container = document.getElementById('heatmapContainer');
    if (!container) return;

    container.innerHTML = '';
    // Find grid dimensions
    const maxRow = Math.max(...heatmapData.map(s => s.seat_row), 1);
    const maxCol = Math.max(...heatmapData.map(s => s.seat_col), 1);

    for (let row = 1; row <= maxRow; row++) {
        for (let col = 1; col <= maxCol; col++) {
            const student = heatmapData.find(s => s.seat_row === row && s.seat_col === col);
            const seat = document.createElement('div');
            seat.className = 'heatmap-seat';

            if (student) {
                seat.style.backgroundColor = student.color;
                seat.style.border = `2px solid ${student.color}`;
                seat.textContent = student.student_id.replace('STU', '');
                seat.title = `${student.name}\n${student.emotion}\nEngagement: ${student.engagement}%`;
                seat.onclick = () => showStudentPopup(student);
            } else {
                seat.classList.add('heatmap-empty');
            }
            container.appendChild(seat);
        }
    }
}

function showStudentPopup(student) {
    const msg = `Student: ${student.name}\nID: ${student.student_id}\nEmotion: ${student.emotion}\nEngagement: ${student.engagement}%\nPresent: ${student.present ? 'Yes' : 'No'}`;
    alert(msg);
}

// ─── Student Cards from Backend ───────────────────────────────────────────────
async function loadStudentsFromBackend() {
    if (!backendConnected) return false;
    const data = await API.get('/students/');
    if (!data || !data.students) return false;

    const container = document.getElementById('studentCardsContainer');
    if (!container) return true;

    container.innerHTML = '';
    data.students.slice(0, 8).forEach(student => {
        const card = document.createElement('div');
        card.className = 'student-card fade-in';

        const eng = Math.round(student.current_engagement);
        const color = eng >= 80 ? 'success' : eng >= 60 ? 'warning' : 'danger';
        const initials = student.name.split(' ').map(n => n[0]).join('').toUpperCase();

        card.innerHTML = `
            <div class="student-avatar">${initials}</div>
            <div class="student-name">${student.name}</div>
            <div class="student-id">${student.student_id}</div>
            <div class="engagement-score">
                <div class="engagement-bar">
                    <div class="engagement-fill" style="width: ${eng}%"></div>
                </div>
                <span class="engagement-text">${eng}%</span>
            </div>
            <span class="badge badge-${color}">${student.current_emotion}</span>
        `;
        container.appendChild(card);
    });
    return true;
}

// ─── Live Monitoring Integration ──────────────────────────────────────────────
let liveDataInterval = null;
let isLiveMode = false;

async function startLiveBackendMonitoring(sessionData) {
    if (!backendConnected) return false;
    isLiveMode = true;

    // Connect video feed
    const videoImg = document.getElementById('liveCameraFeed');
    if (videoImg) {
        videoImg.src = `${MJPEG_URL}?t=${new Date().getTime()}`;
        videoImg.onerror = () => {
            videoImg.style.display = 'none';
            const placeholder = document.getElementById('videoPlaceholder');
            if (placeholder) placeholder.style.display = 'flex';
        };
        videoImg.style.display = 'block';
    }

    // Start polling for live data every 3 seconds
    if (liveDataInterval) clearInterval(liveDataInterval);
    liveDataInterval = setInterval(async () => {
        const liveData = await API.get('/live/data/');
        if (liveData) {
            updateLiveUIFromBackend(liveData);
        }
    }, 3000);

    return true;
}

function stopLiveBackendMonitoring() {
    isLiveMode = false;
    if (liveDataInterval) {
        clearInterval(liveDataInterval);
        liveDataInterval = null;
    }
    const videoImg = document.getElementById('liveCameraFeed');
    if (videoImg) {
        videoImg.src = '';
        videoImg.style.display = 'none';
    }
}

function updateLiveUIFromBackend(data) {
    // Update presence count
    const presentEl = document.getElementById('presentCount');
    if (presentEl) presentEl.textContent = `${data.present_count || 0}/24`;

    // Update average engagement
    const engEl = document.getElementById('liveEngagement');
    if (engEl) engEl.textContent = `${data.avg_engagement || 0}%`;

    // Update emotion counts
    const emo = data.emotion_distribution || {};
    const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setEl('happyCount', emo.happy || 0);
    setEl('neutralCount', emo.neutral || 0);
    setEl('confusedCount', emo.confused || 0);
    setEl('boredCount', emo.bored || 0);

    // Update alerts
    if (data.alerts && data.alerts.length > 0) {
        renderLiveAlerts(data.alerts);
    }

    // Update live student grid from real detection data
    if (data.students && data.students.length > 0) {
        renderDetectedStudents(data.students);
    }

    // Dispatch custom event for other scripts to listen to
    window.dispatchEvent(new CustomEvent('liveDataUpdate', { detail: data }));
}

function renderLiveAlerts(alerts) {
    const container = document.getElementById('liveAlerts');
    if (!container) return;
    container.innerHTML = '';

    const formatAlertTime12h = (alert) => {
        const timeOptions = {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: true,
            timeZone: 'Asia/Kolkata',
        };

        // Prefer ISO timestamp when present.
        if (alert && alert.timestamp) {
            const d = new Date(alert.timestamp);
            if (!Number.isNaN(d.getTime())) {
                return d.toLocaleString('en-IN', timeOptions);
            }
        }

        // Convert plain HH:MM[:SS] (24h) to 12h.
        const raw = String((alert && alert.time) || '').trim();
        const m = raw.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
        if (m) {
            const hour24 = Number(m[1]);
            const minute = m[2];
            const second = m[3] || '00';
            const suffix = hour24 >= 12 ? 'PM' : 'AM';
            const hour12 = hour24 % 12 || 12;
            return `${hour12}:${minute}:${second} ${suffix}`;
        }

        return raw;
    };

    alerts.slice(0, 5).forEach(alert => {
        const div = document.createElement('div');
        const severity = alert.severity || 'medium';
        div.className = `alert alert-${severity === 'high' ? 'danger' : 'warning'} fade-in`;
        const icon = severity === 'high' ? '⚠️' : '⚡';
        const actor = alert.student_name || 'Classroom';
        const when = formatAlertTime12h(alert);
        div.innerHTML = `
            <span>${icon}</span>
            <div>
                <strong>${actor}</strong>
                <p>${alert.message}</p>
                <small>${when}</small>
            </div>
        `;
        container.appendChild(div);
    });
    const badge = document.getElementById('alertCount');
    if (badge) badge.textContent = alerts.length;
}

function renderDetectedStudents(students) {
    const container = document.getElementById('liveStudentGrid');
    if (!container) return;
    container.innerHTML = '';
    students.forEach((student, i) => {
        const card = document.createElement('div');
        card.className = 'student-card fade-in';
        const eng = Math.round(student.engagement_score || 0);
        const color = eng >= 80 ? 'success' : eng >= 60 ? 'warning' : 'danger';
        card.innerHTML = `
            <div class="student-avatar" style="background: var(--${color === 'success' ? 'success' : color === 'warning' ? 'warning' : 'danger'}-color)">
                F${i + 1}
            </div>
            <div class="student-name" style="font-size:0.8rem">Face #${i + 1}</div>
            <div style="font-size:0.7rem; color: var(--text-secondary)">
                ${student.is_looking_forward ? '👀 Attentive' : '👁️ Distracted'}
            </div>
            <div class="engagement-score">
                <div class="engagement-bar">
                    <div class="engagement-fill" style="width: ${eng}%"></div>
                </div>
                <span class="engagement-text">${eng}%</span>
            </div>
            <span class="badge badge-${color}">${student.emotion || 'unknown'}</span>
        `;
        container.appendChild(card);
    });
}

// ─── Session Management ───────────────────────────────────────────────────────
let currentSession = null;

async function startBackendSession(className = 'CS101', subject = 'Computer Science', camera = '0', metadata = {}) {
    if (!backendConnected) return null;

    const teacherData = JSON.parse(localStorage.getItem('currentUser') || '{}');
    const payload = {
        class_name: className,
        subject: subject,
        camera_source: camera,
        teacher_id: teacherData.id || 1,
    };
    if (metadata && typeof metadata === 'object') {
        Object.assign(payload, metadata);
    }

    const data = await API.post('/sessions/start/', {
        ...payload,
    });

    if (data && data.success) {
        currentSession = data.session;
        localStorage.setItem('activeSession', JSON.stringify(currentSession));
        console.log('✅ Session started:', currentSession);
        return currentSession;
    }
    return null;
}

async function endBackendSession() {
    if (!backendConnected || !currentSession) return false;
    const data = await API.post(`/sessions/${currentSession.id}/end/`);
    if (data && data.success) {
        console.log(`✅ Session ended. Duration: ${data.duration} minutes`);
        currentSession = null;
        localStorage.removeItem('activeSession');
        return true;
    }
    return false;
}

// ─── Login Integration ────────────────────────────────────────────────────────
async function loginWithBackend(email, password) {
    if (!backendConnected) {
        // Fallback to local auth (demo mode)
        return { success: true, teacher: { id: 1, name: 'Demo Teacher', email } };
    }
    return await API.post('/auth/login/', { email, password });
}

// ─── Analytics Integration ────────────────────────────────────────────────────
async function loadAnalyticsFromBackend(days = 7) {
    if (!backendConnected) return null;
    return await API.get('/analytics/', { days });
}

async function loadAttendanceFromBackend(date) {
    if (!backendConnected) return null;
    const params = date ? { date } : {};
    return await API.get('/attendance/', params);
}

// ─── Auto-seed Demo Data ──────────────────────────────────────────────────────
async function seedDemoStudents() {
    if (!backendConnected) return;
    const health = await API.get('/health/');
    if (health && health.students === 0) {
        console.log('🌱 Seeding demo student data...');
        const result = await API.post('/setup/seed/');
        if (result && result.success) {
            console.log(`✅ Seeded ${result.students_created} students`);
        }
    }
}

// ─── Dashboard Charts from Backend ───────────────────────────────────────────
async function loadChartsFromBackend() {
    if (!backendConnected || !window.ChartUtils) return;

    const data = await API.get('/dashboard/stats/');
    if (!data) return;

    // Update emotion chart with real data
    if (data.emotion_distribution && Object.keys(data.emotion_distribution).length > 0) {
        const emotionData = data.emotion_distribution;
        window._backendEmotionData = emotionData;
    }
}

// ─── Initialize on DOM Load ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    // Check backend connection first
    await checkBackendConnection();

    // Seed demo data if needed
    await seedDemoStudents();

    // Load dashboard data from backend if on dashboard page
    if (window.location.pathname.includes('dashboard') || document.title.includes('Dashboard')) {
        if (backendConnected) {
            await loadDashboardFromBackend();
            await loadStudentsFromBackend();
        }
    }

    // Load analytics if on analytics page
    if (window.location.pathname.includes('analytics') || document.title.includes('Analytics')) {
        if (backendConnected) {
            const analyticsData = await loadAnalyticsFromBackend();
            if (analyticsData) {
                window._backendAnalytics = analyticsData;
                window.dispatchEvent(new CustomEvent('analyticsLoaded', { detail: analyticsData }));
            }
        }
    }

    // Restore active session if any
    const savedSession = localStorage.getItem('activeSession');
    if (savedSession) {
        currentSession = JSON.parse(savedSession);
    }

    // Keep status fresh in case backend comes online after initial load.
    if (connectionCheckInterval) {
        clearInterval(connectionCheckInterval);
    }
    connectionCheckInterval = setInterval(checkBackendConnection, 15000);
});

window.addEventListener('beforeunload', () => {
    if (connectionCheckInterval) {
        clearInterval(connectionCheckInterval);
        connectionCheckInterval = null;
    }
});

// ─── Export for global use ────────────────────────────────────────────────────
window.SmartClassAPI = {
    API,
    backendConnected: () => backendConnected,
    checkConnection: checkBackendConnection,
    loginWithBackend,
    startSession: startBackendSession,
    endSession: endBackendSession,
    startLiveMonitoring: startLiveBackendMonitoring,
    stopLiveMonitoring: stopLiveBackendMonitoring,
    loadDashboard: loadDashboardFromBackend,
    loadStudents: loadStudentsFromBackend,
    loadAnalytics: loadAnalyticsFromBackend,
    loadAttendance: loadAttendanceFromBackend,
    renderLiveAlerts,
    MJPEG_URL,
};

console.log('🚀 SmartClass API Bridge loaded | Backend:', API_BASE);
