/* MDW 前端交互
 * 1. 侧边栏：桌面端收起/展开（记忆偏好），移动端抽屉开合
 * 2. 分组折叠：点击箭头或目录标题切换（grid 缓动动画），状态持久化并跨页面保持
 * 3. 侧边栏滚动位置记忆：切换页面后恢复滚动位置
 * 4. 顶栏：滚动时浮现阴影
 * 5. Material 涟漪效果（尊重 prefers-reduced-motion）
 */
(function () {
  'use strict';

  var nav = document.getElementById('mdw-nav');
  var navBody = nav && nav.querySelector('.mdw-nav-body');
  var btn = document.getElementById('mdw-sidenav-btn');
  var closeBtn = document.getElementById('mdw-nav-close');
  var backdrop = document.getElementById('mdw-backdrop');
  var appbar = document.getElementById('mdw-appbar');
  var body = document.body;

  var KEY_NAV = 'mdw:nav-collapsed';
  var KEY_PREFIX = 'mdw:collapse:';
  var KEY_SCROLL = 'mdw:sidebar-scroll';
  var mobile = window.matchMedia('(max-width: 899px)');
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function readLS(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function writeLS(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* 隐私模式忽略 */ }
  }

  /* ── 侧边栏：桌面收起 / 移动抽屉 ─────────────────── */
  function setDesktopCollapsed(collapsed) {
    body.classList.toggle('nav-collapsed', !!collapsed);
    if (btn) btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    writeLS(KEY_NAV, collapsed ? '1' : '0');
  }

  function applyDesktopState() {
    if (!mobile.matches) setDesktopCollapsed(readLS(KEY_NAV) === '1');
  }

  function openDrawer() {
    body.classList.add('nav-open');
    if (btn) btn.setAttribute('aria-expanded', 'true');
  }
  function closeDrawer() {
    body.classList.remove('nav-open');
  }

  if (btn) btn.addEventListener('click', function () {
    if (mobile.matches) {
      body.classList.contains('nav-open') ? closeDrawer() : openDrawer();
    } else {
      setDesktopCollapsed(!body.classList.contains('nav-collapsed'));
    }
  });

  if (closeBtn) closeBtn.addEventListener('click', closeDrawer);
  if (backdrop) backdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeDrawer();
  });

  /* ── 分组折叠 ─────────────────────────────────────── */
  function toggleGroup(group, collapsed) {
    group.classList.toggle('collapsed', !!collapsed);
    var toggle = group.querySelector('.mdw-nav-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    writeLS(KEY_PREFIX + (group.getAttribute('data-path') || ''), collapsed ? '1' : '0');
  }

  if (nav) {
    nav.addEventListener('click', function (e) {
      var target = e.target.closest('.mdw-nav-toggle, .mdw-nav-dir');
      if (!target) return;
      var group = target.closest('.mdw-nav-group');
      if (!group) return;
      toggleGroup(group, !group.classList.contains('collapsed'));
    });

    nav.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var target = e.target.closest('.mdw-nav-dir');
      if (!target) return;
      e.preventDefault();
      var group = target.closest('.mdw-nav-group');
      if (!group) return;
      toggleGroup(group, !group.classList.contains('collapsed'));
    });

    // 移动端：点击链接后自动关闭抽屉
    nav.addEventListener('click', function (e) {
      if (mobile.matches && e.target.closest('a')) closeDrawer();
    });
  }

  /* ── 路径匹配工具 ────────────────────────────────── */
  function activePath() {
    if (!nav) return '';
    // 优先从 a.active 获取，兼容 .mdw-nav-dir.active
    var link = nav.querySelector('a.active');
    if (link) return link.getAttribute('href') || '';
    var dir = nav.querySelector('.mdw-nav-dir.active');
    if (dir) {
      var group = dir.closest('.mdw-nav-group');
      return group ? (group.getAttribute('data-path') || '') : '';
    }
    return '';
  }

  function groupContains(group, path) {
    if (!path) return false;
    var dp = (group.getAttribute('data-path') || '').replace(/\/+$/, '');
    var p = path.replace(/\/+$/, '');
    return !!dp && (p === dp || p.indexOf(dp + '/') === 0);
  }

  /* ── 恢复折叠偏好（在DOM解析后立即执行，避免闪烁） ── */
  if (nav) {
    var active = activePath();
    nav.querySelectorAll('.mdw-nav-group[data-path]').forEach(function (group) {
      if (readLS(KEY_PREFIX + group.getAttribute('data-path')) !== '1') return;
      if (groupContains(group, active)) return;
      toggleGroup(group, true);
    });
  }

  // 折叠状态恢复完毕，启用过渡动画
  requestAnimationFrame(function () {
    document.documentElement.classList.remove('mdw-no-transition');
  });

  /* ── 侧边栏滚动位置记忆 ──────────────────────────── */
  function saveScroll() {
    if (navBody) writeLS(KEY_SCROLL, String(navBody.scrollTop || 0));
  }
  function restoreScroll() {
    if (!navBody) return;
    var saved = parseInt(readLS(KEY_SCROLL) || '0', 10);
    if (saved > 0) {
      navBody.scrollTop = saved;
    } else {
      // 无保存值时，滚动到当前激活项可见
      var active = navBody.querySelector('.mdw-nav-link.active, .mdw-nav-dir.active');
      if (active) active.scrollIntoView({ block: 'nearest' });
    }
  }

  // 离开页面前保存滚动位置
  window.addEventListener('beforeunload', saveScroll);
  // 页面加载后恢复
  restoreScroll();

  /* ── 顶栏：滚动时浮现阴影 ────────────────────────── */
  function onScroll() {
    var y = window.pageYOffset || document.documentElement.scrollTop || 0;
    if (appbar) appbar.classList.toggle('scrolled', y > 4);
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  /* ── Material 涟漪效果 ───────────────────────────── */
  function attachRipple(el) {
    if (!el || reduceMotion.matches) return;
    el.addEventListener('pointerdown', function (e) {
      var rect = el.getBoundingClientRect();
      var size = Math.max(rect.width, rect.height) * 2;
      var ripple = document.createElement('span');
      ripple.className = 'mdw-ripple';
      ripple.style.width = ripple.style.height = size + 'px';
      ripple.style.left = (e.clientX - rect.left - size / 2) + 'px';
      ripple.style.top = (e.clientY - rect.top - size / 2) + 'px';
      el.appendChild(ripple);
      ripple.addEventListener('animationend', function () {
        if (ripple.parentNode) ripple.parentNode.removeChild(ripple);
      });
    });
  }

  if (nav) {
    nav.querySelectorAll('.mdw-nav-row').forEach(attachRipple);
  }
  if (btn) attachRipple(btn);
  if (closeBtn) attachRipple(closeBtn);
  Array.prototype.forEach.call(
    document.querySelectorAll('.mdw-dir-listing li a'),
    attachRipple
  );

  /* ── 视口变化同步 ───────────────────────────────── */
  mobile.addEventListener('change', function (e) {
    if (e.matches) {
      closeDrawer();
      body.classList.remove('nav-collapsed');
    } else {
      applyDesktopState();
    }
  });

  /* ── 页面跳转卡片：连续相邻的卡片自动并排为网格 ── */
  function groupPageCards() {
    var cards = document.querySelectorAll(
      '.mdw-content > .mdw-block.mdw-page-card'
    );
    for (var i = 0; i < cards.length; i++) {
      var first = cards[i];
      var parent = first.parentNode;
      if (parent.classList.contains('mdw-page-card-grid')) continue;
      var group = [first];
      var cur = first;
      while (
        cur.nextElementSibling &&
        cur.nextElementSibling.classList.contains('mdw-block') &&
        cur.nextElementSibling.classList.contains('mdw-page-card')
      ) {
        cur = cur.nextElementSibling;
        group.push(cur);
      }
      if (group.length < 2) continue;
      var grid = document.createElement('div');
      grid.className = 'mdw-page-card-grid';
      parent.insertBefore(grid, first);
      group.forEach(function (card) { grid.appendChild(card); });
      i += group.length - 1;
    }
  }
  groupPageCards();

  applyDesktopState();

  /* ── 代码复制（全局可访问） ─────────────────────── */
  window.MDW = window.MDW || {};
  window.MDW.copyCode = function (btn) {
    var block = btn.closest('.mdw-code-block');
    if (!block) return;
    var codeEl = block.querySelector('code');
    if (!codeEl) return;
    var text = codeEl.textContent || '';
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        btn.classList.add('mdw-copied');
        setTimeout(function () { btn.classList.remove('mdw-copied'); }, 1800);
      });
    } else {
      // 回退：创建临时 textarea
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(ta);
      btn.classList.add('mdw-copied');
      setTimeout(function () { btn.classList.remove('mdw-copied'); }, 1800);
    }
  };

  /* ── 标题锚点：点击 # 复制链接 ───────────────────── */
  document.addEventListener('click', function (e) {
    var h = e.target.closest('h1[id],h2[id],h3[id],h4[id],h5[id],h6[id]');
    if (!h || !h.id) return;
    // 仅当点击位置在标题文字左侧（# 图标区域）时触发
    var rect = h.getBoundingClientRect();
    var clickX = e.clientX - rect.left;
    if (clickX > 24) return;  // 超出 # 图标范围不触发
    var url = window.location.origin + window.location.pathname + '#' + h.id;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () {
        showAnchorToast(h);
      });
    } else {
      var ta = document.createElement('textarea');
      ta.value = url;
      ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(ta);
      showAnchorToast(h);
    }
  });

  function showAnchorToast(h) {
    var toast = document.createElement('span');
    toast.className = 'mdw-anchor-toast';
    toast.textContent = '链接已复制';
    var rect = h.getBoundingClientRect();
    toast.style.cssText = 'position:absolute;left:0;top:-24px;font-size:11px;color:var(--primary);opacity:0;transition:opacity 0.2s;white-space:nowrap;pointer-events:none;';
    h.style.position = h.style.position || 'relative';
    h.appendChild(toast);
    requestAnimationFrame(function () { toast.style.opacity = '1'; });
    setTimeout(function () {
      toast.style.opacity = '0';
      setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 200);
    }, 1500);
  }
})();