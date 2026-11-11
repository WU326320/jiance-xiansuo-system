/* Visual-only enhancement. Existing prototype owns all business demonstration state.
 * The sliding-selection idea references LeiBox Glass Tab Menu:
 * https://leibox.pantoria.cn/docs/animations/glass-tab-menu
 * This adapter is independently implemented for existing controls; no upstream
 * component source or six-action menu is embedded.
 */
(() => {
  'use strict';
  const main = document.getElementById('main');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const tracks = new WeakMap();
  let pending = null;

  function place(track, animateFrom) {
    const active = track.querySelector('.tab.active');
    if (!active) return;
    let selection = tracks.get(track);
    if (!selection) {
      selection = document.createElement('span');
      selection.className = 'glass-selection';
      selection.setAttribute('aria-hidden', 'true');
      track.append(selection);
      track.classList.add('glass-ready');
      tracks.set(track, selection);
    }
    const left = active.offsetLeft;
    const width = active.offsetWidth;
    selection.style.width = width + 'px';
    selection.style.height = active.offsetHeight + 'px';
    selection.style.top = active.offsetTop + 'px';
    selection.style.transform = `translateX(${left}px)`;
    if (animateFrom && !reduceMotion.matches) {
      selection.getAnimations().forEach(animation => animation.cancel());
      selection.animate([
        {transform: `translateX(${animateFrom.left}px)`, width: animateFrom.width + 'px'},
        {transform: `translateX(${left}px)`, width: width + 'px'}
      ], {duration: 260, easing: 'cubic-bezier(.2,.8,.2,1)'});
    }
  }

  document.addEventListener('click', event => {
    const tab = event.target.closest('.tabs .tab');
    if (!tab) return;
    const track = tab.closest('.tabs');
    const active = track.querySelector('.tab.active');
    if (active) pending = {
      action: tab.dataset.act,
      value: tab.dataset.value,
      left: active.offsetLeft,
      width: active.offsetWidth,
      scrollLeft: track.scrollLeft
    };
  }, true);

  function refresh() {
    const from = pending;
    pending = null;
    main.querySelectorAll('.tabs').forEach(track => {
      const active = track.querySelector('.tab.active');
      const matched = from && active?.dataset.act === from.action && active?.dataset.value === from.value;
      if (matched) track.scrollLeft = from.scrollLeft;
      place(track, matched ? from : null);
    });
  }
  // Tracks are replaced by the shared renderer; this observes only additions,
  // not style writes, so indicator placement cannot trigger an attribute loop.
  const observer = new MutationObserver(refresh);
  observer.observe(main, {childList: true, subtree: true});
  window.addEventListener('resize', () => {
    main.querySelectorAll('.tabs').forEach(track => place(track));
  }, {passive: true});

  // Arrow keys switch visible segments; Enter/Space keep native button behavior.
  main.addEventListener('keydown', event => {
    const tab = event.target.closest('.tabs .tab');
    if (!tab || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = Array.from(tab.closest('.tabs').querySelectorAll('.tab:not(:disabled)'));
    const index = tabs.indexOf(tab);
    const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1)
      : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    const action = next.dataset.act, value = next.dataset.value;
    next.click();
    queueMicrotask(() => {
      const replacement = Array.from(main.querySelectorAll('.tabs .tab')).find(
        button => button.dataset.act === action && button.dataset.value === value
      );
      replacement?.focus({preventScroll: true});
      replacement?.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'instant'});
    });
  });

  document.getElementById('reduce-transparency').addEventListener('change', event => {
    document.body.dataset.material = event.target.checked ? 'solid' : 'glass';
  });
  reduceMotion.addEventListener('change', () => {
    main.querySelectorAll('.glass-selection').forEach(selection => {
      selection.getAnimations().forEach(animation => animation.cancel());
    });
  });
  refresh();
})();
