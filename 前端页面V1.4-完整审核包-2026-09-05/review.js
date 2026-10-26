(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.V14Review = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const REVIEW_VERSION = 'zhongshu-review-v1.4';

  const PAGES = [
    ['01', '工作台', '01-工作台.png', '工作台', '工作台', '业务页面', '链接或上传视频后，进入文案工坊或视频复刻；文案确认后再进入数字人口播。'],
    ['02', '爆款视频', '02-爆款视频.png', '创作', '爆款视频', '业务页面', '前台仅浏览抖音、视频号的视频内容与创作入口。'],
    ['03', '爆款视频详情', '03-爆款视频详情.png', '创作', '爆款视频', '业务页面', '查看来源摘要，并选择提取文案、视频复刻或收藏。'],
    ['04', '文案工坊', '04-文案工坊.png', '创作', '文案工坊', '业务页面', '原文与二创终稿集中管理，终稿可直接带入数字人口播。'],
    ['05', '视频复刻工作区', '05-视频复刻工作区.png', '创作', '视频创作', '业务页面', '在同一工作区完成来源导入、分镜编辑和镜头生成。'],
    ['06', '人物置换', '06-人物置换.png', '创作', '视频创作', '业务页面', '区分原始画面、目标人物照片和待确认的置换首帧。'],
    ['07', '视频生成-文图生视频', '07-视频生成-文图生视频.png', '创作', '视频创作', '业务页面', '文生视频和图生视频合并，通过是否添加首帧自然切换。'],
    ['08', '视频生成-参考生视频', '08-视频生成-参考生视频.png', '创作', '视频创作', '模式页签', '视频生成的参考素材模式，支持图片、视频和音频引用。'],
    ['09', '数字人口播-用文案生成', '09-数字人口播-用文案生成.png', '创作', '视频创作', '业务页面', '复用终稿、已确认声音和口播分身，不重复创建人物资产。'],
    ['09A', '数字人口播-用已有音频生成', '09A-数字人口播-用已有音频生成.png', '创作', '视频创作', '模式页签', '完整口播音频直接驱动口播分身，不再要求文案或克隆声音。'],
    ['10', '任务中心', '10-任务中心.png', '任务', '任务中心', '业务页面', '统一查看视频复刻、人物置换、视频生成和数字人口播任务。'],
    ['11', '任务详情与结果', '11-任务详情与结果.png', '任务', '任务中心', '业务页面', '查看任务来源、生成结果、素材归档和发布草稿入口。'],
    ['12', '人物库', '12-人物库.png', '资产', '人物库', '业务页面', '每个人仅保留一个档案，汇总定位、照片、分身和声音。'],
    ['13', '人物详情-IP定位', '13-人物详情-IP定位.png', '资产', '人物库', '业务页面', '维护人物的行业身份、受众、服务范围和表达特点。'],
    ['14', '人物详情-形象照片', '14-人物详情-形象照片.png', '资产', '人物库', '模式页签', '一张五视图合成图加多套场景照片，可流转到置换或分身制作。'],
    ['15', '人物详情-口播分身', '15-人物详情-口播分身.png', '资产', '人物库', '模式页签', '用人物视频或已有形象照片制作可驱动的口播分身。'],
    ['16', '人物详情-声音档案', '16-人物详情-声音档案.png', '资产', '人物库', '模式页签', '在人物档案内克隆、试听、确认并设置默认声音。'],
    ['17', '素材库', '17-素材库.png', '资产', '素材库', '业务页面', '统一管理图片、视频、音频和口播成片，并按用途流转。'],
    ['18', '发布管理', '18-发布管理.png', '分发', '发布管理', '业务页面', '生成结果进入发布管理时只创建草稿，不默认自动发布。'],
    ['19', '数据看板', '19-数据看板.png', '分发', '数据看板', '业务页面', '汇总作品、任务和渠道表现，示例数据不代表真实结果。'],
    ['20', '用户档案', '20-用户档案.png', '账户', '用户档案', '业务页面', '用户资料与账户入口保持在侧栏左下角。'],
  ].map(([id, name, file, module, nav, kind, focus]) => ({ id, name, file, module, nav, kind, focus }));

  const CHAINS = {
    文案到口播: ['02', '03', '04', '09', '10', '11', '18'],
    照片到人物置换: ['12', '14', '06', '05', '10'],
    照片到口播分身: ['12', '14', '15', '09'],
    声音到口播: ['12', '16', '09'],
    已有音频到口播: ['17', '09A', '10', '11'],
  };

  function parsePageFilename(filename) {
    const decoded = decodeURIComponent(filename).replace(/\.png$/i, '');
    const separator = decoded.indexOf('-');
    return separator < 0
      ? { id: decoded, title: '' }
      : { id: decoded.slice(0, separator), title: decoded.slice(separator + 1) };
  }

  function storageKey(pageId) {
    return `${REVIEW_VERSION}:${pageId}`;
  }

  function loadReview(storage, pageId) {
    try {
      const value = storage.getItem(storageKey(pageId));
      return value ? JSON.parse(value) : { state: '待审核', note: '' };
    } catch (_) {
      return { state: '待审核', note: '' };
    }
  }

  function saveReview(storage, pageId, review) {
    storage.setItem(storageKey(pageId), JSON.stringify(review));
  }

  function filterPages(pages, filters = {}) {
    const chainIds = filters.chain && CHAINS[filters.chain] ? new Set(CHAINS[filters.chain]) : null;
    const matched = pages.filter((page) => {
      if (filters.module && filters.module !== '全部' && page.module !== filters.module) return false;
      if (filters.kind && filters.kind !== '全部' && page.kind !== filters.kind) return false;
      if (chainIds && !chainIds.has(page.id)) return false;
      if (filters.state && filters.state !== '全部') {
        const state = filters.reviews?.[page.id]?.state || '待审核';
        if (state !== filters.state) return false;
      }
      return true;
    });
    if (!chainIds) return matched;
    const chainOrder = CHAINS[filters.chain];
    return chainOrder.map((id) => matched.find((page) => page.id === id)).filter(Boolean);
  }

  function buildExportPayload(pages, reviews, now = new Date()) {
    const states = pages.map((page) => reviews[page.id]?.state || '待审核');
    return {
      product: '众墅之家｜AI 即创',
      version: '1.4',
      exportedAt: now.toISOString(),
      notice: '静态效果图审核意见，不代表接口已接通或业务已上线。',
      summary: {
        total: pages.length,
        passed: states.filter((state) => state === '通过').length,
        needsChanges: states.filter((state) => state === '需修改').length,
        pending: states.filter((state) => state === '待审核').length,
      },
      pages: pages.map((page) => ({ ...page, review: reviews[page.id] || { state: '待审核', note: '' } })),
    };
  }

  function initializeBrowser() {
    if (typeof document === 'undefined') return;

    const storage = window.localStorage;
    const reviews = Object.fromEntries(PAGES.map((page) => [page.id, loadReview(storage, page.id)]));
    const grid = document.querySelector('#page-grid');
    const directory = document.querySelector('#page-directory');
    const moduleFilter = document.querySelector('#module-filter');
    const stateFilter = document.querySelector('#state-filter');
    const kindFilter = document.querySelector('#kind-filter');
    const chainFilter = document.querySelector('#chain-filter');
    const count = document.querySelector('#visible-count');
    const saveMessage = document.querySelector('#save-message');
    const viewer = document.querySelector('#viewer');
    const viewerImage = document.querySelector('#viewer-image');
    const viewerMissing = document.querySelector('#viewer-missing');
    const viewerTitle = document.querySelector('#viewer-title');
    const viewerOriginal = document.querySelector('#viewer-original');
    let visiblePages = PAGES;
    let viewerIndex = -1;

    function reviewControls(page) {
      const review = reviews[page.id];
      return `
        <label class="field">审核结论
          <select data-state="${page.id}">
            ${['待审核', '通过', '需修改'].map((state) => `<option${state === review.state ? ' selected' : ''}>${state}</option>`).join('')}
          </select>
        </label>
        <label class="field">审核备注
          <textarea data-note="${page.id}" placeholder="写明区域、问题和希望如何调整">${escapeHtml(review.note || '')}</textarea>
        </label>`;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (character) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;',
      })[character]);
    }

    function card(page) {
      return `<article class="page-card" id="page-${page.id}" data-id="${page.id}">
        <button class="thumbnail" type="button" data-open="${page.id}" aria-label="放大查看 ${page.name}">
          <img src="${page.file}" alt="${page.name}效果图">
          <span class="image-status" aria-live="polite">图片未加载，请检查文件是否完整或刷新</span>
        </button>
        <div class="card-body">
          <div class="card-title"><span class="page-id">${page.id}</span><h2>${page.name}</h2><span class="tag">${page.kind}</span></div>
          <p>${page.focus}</p>
          <details><summary>填写审核意见</summary>${reviewControls(page)}</details>
        </div>
      </article>`;
    }

    function renderDirectory() {
      directory.innerHTML = PAGES.map((page) => `<button type="button" data-jump="${page.id}"><span>${page.id}</span>${page.name}</button>`).join('');
    }

    function currentFilters() {
      return {
        module: moduleFilter.value,
        state: stateFilter.value,
        kind: kindFilter.value,
        chain: chainFilter.value,
        reviews,
      };
    }

    function markMissingImages() {
      grid.querySelectorAll('.thumbnail img').forEach((image) => {
        const mark = () => image.closest('.thumbnail').classList.add('missing');
        image.addEventListener('error', mark, { once: true });
        if (image.complete && !image.naturalWidth) mark();
      });
    }

    function renderPages() {
      visiblePages = filterPages(PAGES, currentFilters());
      grid.innerHTML = visiblePages.map(card).join('') || '<div class="empty">当前筛选没有页面。</div>';
      count.textContent = `当前 ${visiblePages.length} / 21 张`;
      markMissingImages();
    }

    function persist(pageId, updateFilteredList = false) {
      const state = grid.querySelector(`[data-state="${pageId}"]`)?.value || reviews[pageId].state;
      const note = grid.querySelector(`[data-note="${pageId}"]`)?.value ?? reviews[pageId].note;
      reviews[pageId] = { state, note };
      try {
        saveReview(storage, pageId, reviews[pageId]);
        saveMessage.textContent = `已保存 ${pageId} 的 V1.4 审核意见`;
      } catch (_) {
        saveMessage.textContent = '浏览器阻止本地保存，请及时导出审核意见 JSON。';
      }
      if (updateFilteredList && stateFilter.value !== '全部') renderPages();
    }

    function openViewer(pageId) {
      const index = visiblePages.findIndex((page) => page.id === pageId);
      if (index < 0) return;
      viewerIndex = index;
      const page = visiblePages[viewerIndex];
      viewerTitle.textContent = `${page.id} · ${page.name}`;
      viewerImage.hidden = false;
      viewerMissing.hidden = true;
      viewerImage.src = page.file;
      viewerImage.alt = `${page.name}原图`;
      viewerOriginal.href = page.file;
      viewerImage.onerror = () => {
        viewerImage.hidden = true;
        viewerMissing.hidden = false;
      };
      viewer.showModal();
    }

    function moveViewer(direction) {
      if (!visiblePages.length) return;
      viewerIndex = (viewerIndex + direction + visiblePages.length) % visiblePages.length;
      openViewer(visiblePages[viewerIndex].id);
    }

    function jumpTo(pageId) {
      moduleFilter.value = '全部';
      stateFilter.value = '全部';
      kindFilter.value = '全部';
      chainFilter.value = '全部';
      renderPages();
      document.querySelector(`#page-${pageId}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function downloadReview() {
      const payload = buildExportPayload(PAGES, reviews);
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `众墅AI即创-V1.4-审核意见-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 0);
    }

    document.querySelectorAll('#module-filter,#state-filter,#kind-filter,#chain-filter').forEach((control) => control.addEventListener('change', renderPages));
    document.querySelector('#export-review').addEventListener('click', downloadReview);
    document.querySelector('#previous-page').addEventListener('click', () => moveViewer(-1));
    document.querySelector('#next-page').addEventListener('click', () => moveViewer(1));
    document.querySelector('#viewer-previous').addEventListener('click', () => moveViewer(-1));
    document.querySelector('#viewer-next').addEventListener('click', () => moveViewer(1));
    document.querySelector('#viewer-close').addEventListener('click', () => viewer.close());

    grid.addEventListener('click', (event) => {
      const open = event.target.closest('[data-open]');
      if (open) openViewer(open.dataset.open);
    });
    grid.addEventListener('change', (event) => {
      const pageId = event.target.dataset.state;
      if (pageId) persist(pageId, true);
    });
    grid.addEventListener('input', (event) => {
      const pageId = event.target.dataset.note;
      if (pageId) persist(pageId);
    });
    directory.addEventListener('click', (event) => {
      const target = event.target.closest('[data-jump]');
      if (target) jumpTo(target.dataset.jump);
    });
    document.addEventListener('keydown', (event) => {
      if (!viewer.open) return;
      if (event.key === 'ArrowLeft') moveViewer(-1);
      if (event.key === 'ArrowRight') moveViewer(1);
    });

    renderDirectory();
    renderPages();
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initializeBrowser);
    else initializeBrowser();
  }

  return {
    CHAINS,
    PAGES,
    REVIEW_VERSION,
    buildExportPayload,
    filterPages,
    loadReview,
    parsePageFilename,
    saveReview,
    storageKey,
  };
});
