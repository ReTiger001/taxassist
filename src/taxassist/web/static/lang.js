/* ── 全站语言切换 ────────────────────────────────────────────────
   关于页原先只有一套**页内**切换（切完就丢、出了那一页就回到中文）。
   这里做成全站的，三个要点：

     ① 语言存 localStorage —— 跳页与刷新都不丢，这是"全站"的前提；
     ② 每个页面加载时对所有 [data-zh][data-en] 元素应用一次；
     ③ 顶栏的切换按钮始终可见（未登录也能切）。

   排版上的考虑：英文普遍比中文长（"Tax Assistant" vs "税务助手"、
   "Daily Brief" vs "每日简报"），所以相关元素一律**自适应宽度 + 不换行**，
   而不是给固定宽度 —— 固定宽度会在中文下显得空、英文下挤成一团。
   见 CSS 里 .top nav a 与 .lang-toggle 的注释。

   没写进模板的文案（如 JS 动态生成的）不参与切换 —— 那是后续的事，
   当前先把静态界面文案覆盖全。

   ── 为什么单独一个文件（而不是内联在 base.html 里）────────────────
   登录页与注册页用的是 auth_base.html —— 它**不继承 base.html**
   （那一页的使用者还没进来，不需要导航与检索）。所以把这段脚本内联
   在 base.html 里，登录页就永远拿不到语言切换，而未登录访客第一眼
   看到的恰恰就是登录页。抽成 /static/lang.js 后两个模板共用一份，
   也免得以后改逻辑要改两处、改漏一处。
   注意：这是**本站自己的**静态文件，不是 CDN，不违反"不引用外部资源"。 */
(function () {
  var KEY = 'taxassist_lang';

  function apply(lang) {
    document.documentElement.lang = (lang === 'en') ? 'en' : 'zh-CN';
    document.querySelectorAll('[data-zh][data-en]').forEach(function (el) {
      var v = el.getAttribute(lang === 'en' ? 'data-en' : 'data-zh');
      if (v !== null) el.textContent = v;
    });
    // 关于页用的是**整块切换**（.lang-block，两段完整内容切显示），
    // 与上面的元素级替换是两套机制 —— 这里一并接管，免得两种状态打架。
    document.querySelectorAll('.lang-block[data-lang]').forEach(function (blk) {
      blk.hidden = (blk.getAttribute('data-lang') !== lang);
    });
    document.querySelectorAll('.lang-btn').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-lang') === lang));
    });
    // **placeholder 要单独处理**：它不是文本子节点，textContent 改不动，
    // 只能用 setAttribute。所以给它一套并行属性（data-ph-zh / data-ph-en）——
    // 输入框的提示语是核心交互的一部分（助手页怎么问、检索页搜什么），
    // 漏掉它们会让英文界面像"半成品"。
    document.querySelectorAll('[data-ph-zh][data-ph-en]').forEach(function (el) {
      var v = el.getAttribute(lang === 'en' ? 'data-ph-en' : 'data-ph-zh');
      if (v !== null) el.setAttribute('placeholder', v);
    });
    var btn = document.getElementById('lang-toggle');
    if (btn) btn.textContent = (lang === 'en') ? '中文' : 'EN';
    // **悬停提示（title）也要双语**。它在这里不是装饰：效力徽章旁边那些
    // "官方标注 / 据他文判定 / 未发现废止" 的解释写的就是"这条判断有多硬"，
    // 是整套证据链的免责说明。界面切成英文而免责说明还是中文，等于对读英文
    // 的人少给了一层信息。用 data-title-zh / data-title-en 并行属性，
    // 机制与 placeholder 相同（title 同样不是文本子节点）。
    document.querySelectorAll('[data-title-zh][data-title-en]').forEach(function (el) {
      var v = el.getAttribute(lang === 'en' ? 'data-title-en' : 'data-title-zh');
      if (v !== null) el.setAttribute('title', v);
    });
    // **无障碍标签也要跟着走**。顶栏搜索框读的是 aria-label，屏幕阅读器
    // 念出来的就是它 —— 界面切成英文而这行还是中文，等于对用读屏的人没切。
    // 和 placeholder、title 同理：aria-label 也不是文本子节点。
    document.querySelectorAll('[data-aria-zh][data-aria-en]').forEach(function (el) {
      var v = el.getAttribute(lang === 'en' ? 'data-aria-en' : 'data-aria-zh');
      if (v !== null) el.setAttribute('aria-label', v);
    });
  }

  // **提交前的确认框也要双语，而且不能靠替换 textContent**。
  // 后台的「删除账号 / 吊销邀请码」把提示写在 onsubmit="return confirm('…')" 里
  // —— 那是 JS 字符串，不是文本节点，data-zh/data-en 根本够不着；而且
  // 属性值是模板渲染时就拼好的，没法在运行时按语言挑。
  // 所以改成让表单自己带 data-confirm-zh / data-confirm-en，来这里按当前
  // 语言取值。用捕获阶段监听，才能在任何内联 onsubmit 之前拿到控制权。
  document.addEventListener('submit', function (ev) {
    var f = ev.target;
    if (!f || !f.getAttribute) return;
    var zh = f.getAttribute('data-confirm-zh');
    var en = f.getAttribute('data-confirm-en');
    if (!zh && !en) return;
    var msg = (lang === 'en') ? (en || zh) : (zh || en);
    if (msg && !window.confirm(msg)) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }
  }, true);

  var lang = 'zh';
  try {
    // 已有选择 → 用它；**首次访问 → 跟随浏览器语言**
    // （中文环境给中文，其余给英文）。这个判断原本只写在关于页的
    // 页内脚本里，全站化之后必须收上来，否则别的页面会把它盖掉。
    var saved = localStorage.getItem(KEY);
    if (saved === 'zh' || saved === 'en') {
      lang = saved;
    } else {
      lang = (navigator.language || '').toLowerCase().indexOf('zh') === 0
        ? 'zh' : 'en';
    }
  } catch (e) { /* 无痕模式：退回中文 */ }

  apply(lang);

  // 顶栏按钮与关于页的「中文/English」都走同一套状态
  document.addEventListener('click', function (ev) {
    var t = ev.target && ev.target.closest
      ? ev.target.closest('#lang-toggle, .lang-btn') : null;
    if (!t) return;
    lang = t.classList.contains('lang-btn')
      ? (t.getAttribute('data-lang') || 'zh')
      : ((lang === 'en') ? 'zh' : 'en');
    try { localStorage.setItem(KEY, lang); } catch (e) {}
    apply(lang);
  });
})();
