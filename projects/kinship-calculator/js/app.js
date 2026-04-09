/**
 * Chinese Kinship Calculator - UI Logic
 */
(function () {
  'use strict';

  let chain = []; // Array of relation IDs (e.g. ['m', 'f', 's'])

  // DOM elements
  const chainContainer = document.getElementById('chain-container');
  const chainEmpty = document.getElementById('chain-empty');
  const resultSection = document.getElementById('result-section');
  const resultTerm = document.getElementById('result-term');
  const resultExplanation = document.getElementById('result-explanation');
  const resultDefault = document.getElementById('result-default');
  const buttonsGrid = document.getElementById('buttons-grid');
  const undoBtn = document.getElementById('undo-btn');
  const resetBtn = document.getElementById('reset-btn');

  function initButtons() {
    const relations = KinshipEngine.getRelations();
    relations.forEach(function (rel) {
      var btn = document.createElement('button');
      btn.className = 'rel-btn';
      btn.textContent = rel.label;
      btn.dataset.id = rel.id;
      btn.addEventListener('click', function () { addRelation(rel.id); });
      buttonsGrid.appendChild(btn);
    });
  }

  function addRelation(id) {
    if (chain.length >= 8) return;
    chain.push(id);
    updateUI();
  }

  function undo() {
    if (chain.length === 0) return;
    chain.pop();
    updateUI();
  }

  function reset() {
    chain = [];
    updateUI();
  }

  function truncateAt(index) {
    chain = chain.slice(0, index + 1);
    updateUI();
  }

  function updateUI() {
    renderChain();
    renderResult();
    undoBtn.disabled = chain.length === 0;
    resetBtn.disabled = chain.length === 0;
  }

  function renderChain() {
    // Remove old pills and arrows
    var old = chainContainer.querySelectorAll('.chain-pill, .chain-arrow');
    old.forEach(function (el) { el.remove(); });

    if (chain.length === 0) {
      chainEmpty.style.display = '';
      return;
    }
    chainEmpty.style.display = 'none';

    chain.forEach(function (id, index) {
      if (index > 0) {
        var arrow = document.createElement('span');
        arrow.className = 'chain-arrow';
        arrow.textContent = '→';
        chainContainer.appendChild(arrow);
      }

      var pill = document.createElement('button');
      pill.className = 'chain-pill';
      pill.textContent = KinshipEngine.getLabel(id);
      pill.title = '点击截断到这一步';
      pill.addEventListener('click', function () { truncateAt(index); });

      // Entrance animation
      pill.style.opacity = '0';
      pill.style.transform = 'scale(0.8)';
      chainContainer.appendChild(pill);
      requestAnimationFrame(function () {
        pill.style.opacity = '1';
        pill.style.transform = 'scale(1)';
      });
    });
  }

  function renderResult() {
    if (chain.length === 0) {
      resultSection.classList.remove('has-result');
      resultDefault.style.display = '';
      resultTerm.style.display = 'none';
      resultExplanation.style.display = 'none';
      return;
    }

    var result = KinshipEngine.resolve(chain);
    resultDefault.style.display = 'none';
    resultTerm.style.display = '';
    resultExplanation.style.display = '';
    resultSection.classList.add('has-result');

    if (result) {
      resultTerm.textContent = result.term;
      resultTerm.classList.remove('not-found');
      resultExplanation.textContent = result.explanation;
    } else {
      resultTerm.textContent = '关系较远，无固定称呼';
      resultTerm.classList.add('not-found');
      resultExplanation.textContent = '该关系链无法简化为常用亲属称谓';
    }

    // Animate result change
    resultTerm.style.opacity = '0';
    resultTerm.style.transform = 'translateY(10px)';
    requestAnimationFrame(function () {
      resultTerm.style.opacity = '1';
      resultTerm.style.transform = 'translateY(0)';
    });
  }

  undoBtn.addEventListener('click', undo);
  resetBtn.addEventListener('click', reset);

  initButtons();
  updateUI();
})();
