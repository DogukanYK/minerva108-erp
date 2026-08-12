/* Yorum formu — sesli not kaydı (MediaRecorder) + progressive-enhancement gönderim.
   JS hiç çalışmazsa/mikrofon desteklenmezse form yine de native POST ile
   çalışır (yazılı yorum + puan) — bu dosya yalnız ses kaydını ekler. */
(function () {
  'use strict';

  var MAX_MS = 120000;          // 2 dk sert limit
  var form = document.getElementById('reviewForm');
  if (!form) return;

  var recBtn = document.getElementById('recBtn');
  var recBtnLabel = document.getElementById('recBtnLabel');
  var recStatus = document.getElementById('recStatus');
  var recPlayer = document.getElementById('recPlayer');
  var recAudio = document.getElementById('recAudio');
  var recRedo = document.getElementById('recRedo');
  var recFallback = document.getElementById('recFallback');
  var recFileInput = document.getElementById('recFileInput');
  var submitBtn = document.getElementById('submitBtn');

  var mediaRecorder = null;
  var stream = null;
  var chunks = [];
  var recordedBlob = null;
  var recordedMime = '';
  var recordedDurationMs = 0;
  var startedAt = 0;
  var timerId = null;
  var maxTimerId = null;

  var supported = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  if (!supported) {
    recBtn.style.display = 'none';
    recFallback.style.display = 'block';
    return;
  }

  function pickMime() {
    var candidates = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', ''];
    for (var i = 0; i < candidates.length; i++) {
      if (candidates[i] === '' || MediaRecorder.isTypeSupported(candidates[i])) return candidates[i];
    }
    return '';
  }

  function fmtTime(ms) {
    var s = Math.floor(ms / 1000);
    var m = Math.floor(s / 60);
    s = s % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  function stopTracks() {
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
  }

  function clearTimers() {
    if (timerId) { clearInterval(timerId); timerId = null; }
    if (maxTimerId) { clearTimeout(maxTimerId); maxTimerId = null; }
  }

  function setIdle() {
    recBtn.classList.remove('stop');
    recBtn.innerHTML = '<i class="bi bi-mic-fill"></i><span>Kaydet</span>';
    recBtn.disabled = false;
  }

  function permissionErrorMessage(err) {
    if (err && err.name === 'NotAllowedError') {
      return 'Mikrofon izni reddedildi. Tarayıcı ayarlarından (adres çubuğundaki kilit simgesi) mikrofon iznini açıp tekrar deneyin.';
    }
    if (err && err.name === 'NotFoundError') {
      return 'Mikrofon bulunamadı.';
    }
    if (err && err.name === 'NotReadableError') {
      return 'Mikrofon başka bir uygulama tarafından kullanılıyor.';
    }
    return 'Mikrofona erişilemedi.';
  }

  async function startRecording() {
    recBtn.disabled = true;
    recStatus.textContent = 'Tarayıcınızın mikrofon iznini bekliyoruz…';
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      recStatus.textContent = permissionErrorMessage(err);
      recBtn.disabled = false;
      return;
    }

    recordedMime = pickMime();
    try {
      mediaRecorder = recordedMime ? new MediaRecorder(stream, { mimeType: recordedMime }) : new MediaRecorder(stream);
    } catch (_e) {
      mediaRecorder = new MediaRecorder(stream);
      recordedMime = '';
    }
    chunks = [];

    mediaRecorder.ondataavailable = function (e) { if (e.data && e.data.size > 0) chunks.push(e.data); };
    mediaRecorder.onstop = function () {
      clearTimers();
      stopTracks();
      recordedDurationMs = Date.now() - startedAt;
      recordedBlob = new Blob(chunks, { type: recordedMime || 'audio/webm' });
      var url = URL.createObjectURL(recordedBlob);
      recAudio.src = url;
      recPlayer.style.display = 'block';
      recStatus.textContent = '';
      setIdle();
    };

    mediaRecorder.start(1000);
    startedAt = Date.now();
    recBtn.disabled = false;
    recBtn.classList.add('stop');
    recBtn.innerHTML = '<i class="bi bi-stop-fill"></i><span>Durdur</span>';

    timerId = setInterval(function () {
      recStatus.textContent = fmtTime(Date.now() - startedAt);
    }, 250);
    maxTimerId = setTimeout(function () {
      if (mediaRecorder && mediaRecorder.state === 'recording') mediaRecorder.stop();
    }, MAX_MS);
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state === 'recording') mediaRecorder.stop();
  }

  recBtn.addEventListener('click', function () {
    if (mediaRecorder && mediaRecorder.state === 'recording') stopRecording();
    else startRecording();
  });

  recRedo.addEventListener('click', function () {
    recordedBlob = null;
    if (recAudio.src) { URL.revokeObjectURL(recAudio.src); recAudio.src = ''; }
    recPlayer.style.display = 'none';
    recStatus.textContent = '';
  });

  // ── Gönderim: ses kaydı varsa JS fetch ile FormData'ya ekler; yoksa
  //    native form POST'u olduğu gibi devam eder (progressive enhancement). ──
  form.addEventListener('submit', function (evt) {
    var fileInputHasFile = recFileInput && recFileInput.files && recFileInput.files.length > 0;
    if (!recordedBlob && !fileInputHasFile) return;   // native submit devam etsin

    evt.preventDefault();
    submitBtn.disabled = true;
    submitBtn.textContent = 'Gönderiliyor…';

    var fd = new FormData(form);
    if (recordedBlob) {
      var ext = (recordedMime || '').indexOf('mp4') !== -1 ? 'm4a' : 'webm';
      fd.append('audio', recordedBlob, 'kayit.' + ext);
      fd.append('audio_duration_s', String(Math.round(recordedDurationMs / 1000)));
    }
    // fileInputHasFile durumunda FormData(form) zaten dosyayı içeriyor
    // (recFileInput'un name'i "audio" olmalı — bkz. HTML).

    fetch(form.action, { method: 'POST', body: fd })
      .then(function (r) {
        if (r.redirected || r.ok) { window.location.href = r.url || form.action.replace('/gonder', ''); return; }
        return r.text().then(function () {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Yorumu Gönder';
          recStatus.textContent = 'Gönderilemedi, lütfen formu kontrol edip tekrar deneyin.';
        });
      })
      .catch(function () {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Yorumu Gönder';
        recStatus.textContent = 'Bağlantı hatası, lütfen tekrar deneyin.';
      });
  });
})();
