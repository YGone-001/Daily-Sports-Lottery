/**
 * 每日体彩预测终端 - 前端交互
 */
document.addEventListener('DOMContentLoaded', function () {
    animateBars();
});

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
