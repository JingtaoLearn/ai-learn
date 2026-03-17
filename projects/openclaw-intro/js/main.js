// ===== Particle Background =====
(function initParticles() {
  var canvas = document.getElementById('particles');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');

  var width, height, particles;
  var PARTICLE_COUNT = 30;
  var CONNECT_DIST = 100;

  function resize() {
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
  }

  function createParticle() {
    return {
      x: Math.random() * width,
      y: Math.random() * height,
      r: Math.random() * 2 + 0.8,
      speedY: -(Math.random() * 0.3 + 0.1),
      speedX: (Math.random() - 0.5) * 0.2,
      opacity: Math.random() * 0.3 + 0.1,
    };
  }

  function init() {
    resize();
    particles = Array.from({ length: PARTICLE_COUNT }, createParticle);
  }

  function draw() {
    ctx.clearRect(0, 0, width, height);
    for (var i = 0; i < particles.length; i++) {
      for (var j = i + 1; j < particles.length; j++) {
        var dx = particles[i].x - particles[j].x;
        var dy = particles[i].y - particles[j].y;
        var dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < CONNECT_DIST) {
          var alpha = (1 - dist / CONNECT_DIST) * 0.12;
          ctx.strokeStyle = 'rgba(52, 152, 219, ' + alpha + ')';
          ctx.lineWidth = 0.5;
          ctx.beginPath();
          ctx.moveTo(particles[i].x, particles[i].y);
          ctx.lineTo(particles[j].x, particles[j].y);
          ctx.stroke();
        }
      }
    }
    for (var k = 0; k < particles.length; k++) {
      var p = particles[k];
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(52, 152, 219, ' + p.opacity + ')';
      ctx.fill();
    }
  }

  function update() {
    for (var k = 0; k < particles.length; k++) {
      var p = particles[k];
      p.y += p.speedY;
      p.x += p.speedX;
      if (p.y < -10) { p.y = height + 10; p.x = Math.random() * width; }
      if (p.x < -10) p.x = width + 10;
      if (p.x > width + 10) p.x = -10;
    }
  }

  function loop() {
    update();
    draw();
    requestAnimationFrame(loop);
  }

  window.addEventListener('resize', resize);
  init();
  loop();
})();

// ===== Slide Navigation Core =====
(function initDeck() {
  var deck = document.getElementById('deck');
  var slides = deck.querySelectorAll('.slide');
  var dots = document.querySelectorAll('#dot-nav button');
  var counter = document.getElementById('slide-counter');
  var total = slides.length;
  var currentSlide = 0;
  var isScrolling = false;

  function goToSlide(index) {
    if (index < 0 || index >= total || isScrolling) return;
    isScrolling = true;
    currentSlide = index;
    updateUI(index);

    // Scroll the slide into view within #deck
    slides[index].scrollIntoView({ behavior: 'smooth', block: 'start' });

    // Reset scrolling lock after animation
    setTimeout(function() { isScrolling = false; }, 800);
  }

  function updateUI(idx) {
    dots.forEach(function(d, i) { d.classList.toggle('active', i === idx); });
    counter.textContent = (idx + 1) + ' / ' + total;

    // Trigger animations for current slide
    slides[idx].querySelectorAll('.anim-in').forEach(function(el) {
      el.classList.add('visible');
    });
  }

  // Detect current slide from scroll position
  deck.addEventListener('scroll', function() {
    var scrollTop = deck.scrollTop;
    var slideHeight = deck.clientHeight;
    var idx = Math.round(scrollTop / slideHeight);
    if (idx !== currentSlide && idx >= 0 && idx < total) {
      currentSlide = idx;
      updateUI(idx);
    }
  }, { passive: true });

  // Initialize first slide
  updateUI(0);

  // Dot click navigation
  dots.forEach(function(dot) {
    dot.addEventListener('click', function() {
      var idx = parseInt(dot.dataset.slide, 10);
      goToSlide(idx);
    });
  });

  // Keyboard navigation
  document.addEventListener('keydown', function(e) {
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowRight':
      case ' ':
      case 'PageDown':
        e.preventDefault();
        goToSlide(currentSlide + 1);
        break;
      case 'ArrowUp':
      case 'ArrowLeft':
      case 'PageUp':
        e.preventDefault();
        goToSlide(currentSlide - 1);
        break;
      case 'Home':
        e.preventDefault();
        goToSlide(0);
        break;
      case 'End':
        e.preventDefault();
        goToSlide(total - 1);
        break;
      case 'f':
      case 'F':
        toggleFullscreen();
        break;
    }
  });

  // Mouse wheel — prevent free scroll, snap to slides
  deck.addEventListener('wheel', function(e) {
    e.preventDefault();
    if (isScrolling) return;
    if (e.deltaY > 0) {
      goToSlide(currentSlide + 1);
    } else if (e.deltaY < 0) {
      goToSlide(currentSlide - 1);
    }
  }, { passive: false });

  // Touch swipe support
  var touchStartY = 0;

  deck.addEventListener('touchstart', function(e) {
    touchStartY = e.touches[0].clientY;
  }, { passive: true });

  deck.addEventListener('touchend', function(e) {
    var dy = touchStartY - e.changedTouches[0].clientY;
    if (Math.abs(dy) > 50) {
      if (dy > 0) {
        goToSlide(currentSlide + 1);
      } else {
        goToSlide(currentSlide - 1);
      }
    }
  }, { passive: true });

  // Fullscreen toggle
  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(function() {});
    } else {
      document.exitFullscreen();
    }
  }
})();
