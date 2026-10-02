/**
 * 每日体彩预测终端 - 前端交互
 */
document.addEventListener('DOMContentLoaded', function () {
    animateBars();
    startClock();
});

/** 北京时间（UTC+8）实时时钟 */
function beijingNow(base) {
    var d = base || new Date();
    var t = d.getTime();
    // 先把本地时区偏移归零，再整体 +8 小时，得到「视作本地」的北京时间
    return new Date(t + d.getTimezoneOffset() * 60000 + 8 * 3600000);
}

function pad2(n) {
    return n < 10 ? '0' + n : String(n);
}

/** 每秒刷新页面上所有北京时间 */
function tickClock() {
    var bj = beijingNow();
    var clock = document.getElementById('live-clock');
    if (clock) {
        clock.textContent =
            pad2(bj.getHours()) + ':' + pad2(bj.getMinutes()) + ':' + pad2(bj.getSeconds());
    }
}

function startClock() {
    tickClock();
    setInterval(tickClock, 1000);
}

/** 概率条入场动画 */
function animateBars() {
    document.querySelectorAll('.mc-bar-seg[data-width]').forEach(function (seg, i) {
        var w = seg.dataset.width;
        seg.style.width = '0%';
        setTimeout(function () { seg.style.width = w + '%'; }, 150 + i * 60);
    });
}

/** 手动触发抓取 */
function triggerRefresh(btn) {
    btn.classList.add('busy');
    var original = btn.innerText;
    btn.innerText = '同步中...';
    fetch('/api/refresh', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            btn.innerText = '已同步 ' + (d.added || 0) + ' 场';
            setTimeout(function () {
                btn.classList.remove('busy');
                btn.innerText = original;
                location.reload();
            }, 900);
        })
        .catch(function () {
            btn.innerText = '同步失败';
            setTimeout(function () {
                btn.classList.remove('busy');
                btn.innerText = original;
            }, 1500);
        });
}
