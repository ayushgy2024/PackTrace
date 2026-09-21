(() => {
  const $ = (selector) => document.querySelector(selector)
  const $$ = (selector) => [...document.querySelectorAll(selector)]
  const modal = $('#capture-modal')
  const codeStep = $('#code-step')
  const cameraStep = $('#camera-step')
  const reviewStep = $('#review-step')
  const orderInput = $('#order-code')
  const openCameraButton = $('#open-camera')
  const recordButton = $('#record-button')
  const preview = $('#camera-preview')
  const reviewVideo = $('#review-video')
  const cameraMessage = $('#camera-message')
  const recordState = $('#record-state')
  const scanFeedback = $('#scan-feedback')
  const torchButton = $('#torch-button')
  const cameraElapsed = $('#camera-elapsed')
  let evidenceType = 'OUTBOUND'
  let stream = null
  let recorder = null
  let chunks = []
  let recordedBlob = null
  let elapsed = 0
  let recordTimer = null
  let scanTimer = null
  let detector = null
  const scanCanvas = document.createElement('canvas')
  let scanAttempts = 0
  let serverScanInFlight = false
  let scannerFailures = 0
  let recordingStopArmed = false
  let recordingAbsentFrames = 0
  let recordingAbsentSince = 0
  let recordingCodeSeenInCycle = false
  let recordingStartedAt = 0
  let autoSaveAfterStop = false
  let recordingSessionId = ''
  let stopReason = 'MANUAL'
  let recordingEndAnnounced = false
  let preferredSpeechVoice = null
  let keyboardScanBuffer = ''
  let keyboardScanLastAt = 0
  let clockTimer = null
  let currentLocation = null
  let torchTrack = null
  let torchEnabled = false
  let activeDriveUpload = null
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || ''
  const driveConnected = document.body.dataset.driveConnected === 'true'
  const currentUserSub = document.body.dataset.userSub || ''
  const uploadDatabaseName = 'packtrace-upload-queue-v1'

  const csrfHeaders = (headers = {}) => ({ ...headers, 'X-CSRF-Token': csrfToken })

  function openUploadDatabase() {
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) return reject(new Error('Offline upload storage is unavailable in this browser.'))
      const request = indexedDB.open(uploadDatabaseName, 1)
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains('uploads')) request.result.createObjectStore('uploads', { keyPath: 'uploadId' })
      }
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error || new Error('The upload queue could not be opened.'))
    })
  }

  async function uploadQueueRequest(mode, action) {
    const database = await openUploadDatabase()
    try {
      return await new Promise((resolve, reject) => {
        const request = action(database.transaction('uploads', mode).objectStore('uploads'))
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error || new Error('The upload queue could not be updated.'))
      })
    } finally {
      database.close()
    }
  }

  const queueUpload = (upload) => uploadQueueRequest('readwrite', (store) => store.put(upload))
  const removeQueuedUpload = (uploadId) => uploadQueueRequest('readwrite', (store) => store.delete(uploadId))
  const listQueuedUploads = () => uploadQueueRequest('readonly', (store) => store.getAll())

  async function persistQueuedUpload(upload) {
    try {
      await queueUpload(upload)
      return true
    } catch {
      return false
    }
  }

  async function sha256Hex(blob) {
    const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
    return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('')
  }

  async function readJsonResponse(response) {
    const responseText = await response.text()
    let result = {}
    try { result = responseText ? JSON.parse(responseText) : {} } catch { result = {} }
    if (!response.ok) throw new Error(result.error || `Request failed (server response ${response.status}).`)
    return result
  }

  const cleanCode = (value) => value.trim().toUpperCase().replace(/\s+/g, '-').replace(/[^A-Z0-9\-_/.]/g, '')
  const normalizedCode = () => cleanCode(orderInput.value)
  const formatTime = (value) => `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`
  const newSessionId = () => crypto.randomUUID?.() || 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (char) => {
    const value = Math.floor(Math.random() * 16)
    return (char === 'x' ? value : (value & 3) | 8).toString(16)
  })

  function jdPackage(value) {
    const match = cleanCode(value).match(/^(JD[A-Z0-9]+)-([1-9][0-9]*)-([1-9][0-9]*)-$/)
    if (!match || Number(match[2]) > Number(match[3])) return null
    return { waybill: match[1], packageCode: match[0] }
  }

  function sameRecordingCode(left, right) {
    const a = cleanCode(left)
    const b = cleanCode(right)
    if (a === b) return true
    const packageA = jdPackage(a)
    const packageB = jdPackage(b)
    return Boolean((packageA && packageA.waybill === b) || (packageB && packageB.waybill === a))
  }

  function extractTrackingCode(rawValue) {
    const raw = String(rawValue || '').trim()
    if (!raw) return ''
    const keys = ['awb', 'tracking', 'tracking_number', 'trackingNumber', 'waybill', 'lrn', 'shipment', 'order_id', 'orderId']
    try {
      const payload = JSON.parse(raw)
      for (const key of keys) {
        const value = cleanCode(String(payload?.[key] || ''))
        if (value.length >= 3 && value.length <= 64) return value
      }
    } catch {
      // Most barcode payloads are not JSON.
    }
    try {
      const url = new URL(raw)
      for (const key of keys) {
        const value = cleanCode(url.searchParams.get(key) || '')
        if (value.length >= 3 && value.length <= 64) return value
      }
      const ignoredSegments = new Set(['TRACK', 'TRACKING', 'SHIPMENT', 'ORDER', 'AWB', 'WAYBILL'])
      const segments = url.pathname.split('/').map((part) => cleanCode(decodeURIComponent(part))).filter(Boolean).reverse()
      for (const value of segments) {
        if (!ignoredSegments.has(value) && value.length >= 3 && value.length <= 64 && /\d/.test(value)) return value
      }
    } catch {
      // Most barcodes contain a plain tracking number rather than a URL.
    }
    const gs1 = raw.match(/(?:\]C1)?(?:\(00\)|00)\s*(\d{18})/)
    if (gs1) return gs1[1]
    const labeled = raw.match(/(?:AWB|TRACKING(?:[_\s-]?NUMBER)?|WAYBILL|LRN|SHIPMENT|ORDER(?:[_\s-]?ID)?)[\s:=#"'-]*([A-Z0-9][A-Z0-9/_-]{2,63})/i)
    if (labeled) return cleanCode(labeled[1])
    if (/^[A-Z0-9][A-Z0-9\-_/.\s]{2,63}$/i.test(raw)) return cleanCode(raw)
    return ''
  }

  function showToast(message, isError = false) {
    const toast = $('#toast')
    toast.textContent = message
    toast.style.background = isError ? '#8f3434' : '#18382b'
    toast.hidden = false
    window.setTimeout(() => { toast.hidden = true }, 3400)
  }

  function selectFemaleSpeechVoice() {
    if (!('speechSynthesis' in window)) return null
    const voices = window.speechSynthesis.getVoices()
    if (!voices.length) return null
    const femaleNames = /female|veena|neerja|heera|isha|samantha|victoria|karen|moira|tessa|zira|aria|jenny|susan|hazel|ava|serena/i
    const englishVoices = voices.filter((voice) => /^en(?:-|$)/i.test(voice.lang))
    preferredSpeechVoice = englishVoices.find((voice) => /^en-IN$/i.test(voice.lang) && femaleNames.test(voice.name))
      || englishVoices.find((voice) => femaleNames.test(voice.name))
      || englishVoices.find((voice) => /^en-IN$/i.test(voice.lang))
      || englishVoices[0]
      || null
    return preferredSpeechVoice
  }

  if ('speechSynthesis' in window) {
    selectFemaleSpeechVoice()
    window.speechSynthesis.addEventListener?.('voiceschanged', selectFemaleSpeechVoice)
  }

  function announceRecordingStatus(message) {
    if (!('speechSynthesis' in window) || typeof window.SpeechSynthesisUtterance !== 'function') return
    try {
      window.speechSynthesis.cancel()
      const announcement = new window.SpeechSynthesisUtterance(message)
      const voice = preferredSpeechVoice || selectFemaleSpeechVoice()
      if (voice) announcement.voice = voice
      const voiceLanguage = String(voice?.lang || '')
      announcement.lang = /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/i.test(voiceLanguage) ? voiceLanguage : 'en-IN'
      announcement.rate = 0.95
      announcement.pitch = voice && /female|veena|neerja|heera|isha|samantha|victoria|karen|moira|tessa|zira|aria|jenny|susan|hazel|ava|serena/i.test(voice.name) ? 1 : 1.15
      announcement.volume = 1
      window.speechSynthesis.speak(announcement)
    } catch {
      // Voice feedback is optional and must never interrupt recording or saving.
    }
  }

  function announceRecordingEnd() {
    if (recordingEndAnnounced) return
    recordingEndAnnounced = true
    announceRecordingStatus('Recording End')
  }

  function updateCameraClock() {
    const now = new Date()
    $('#camera-clock').textContent = now.toLocaleTimeString([], { hour12: false })
    const year = now.getFullYear()
    const month = String(now.getMonth() + 1).padStart(2, '0')
    const day = String(now.getDate()).padStart(2, '0')
    $('#camera-date').textContent = `${year}-${month}-${day}`
  }

  function startCameraClock() {
    window.clearInterval(clockTimer)
    updateCameraClock()
    clockTimer = window.setInterval(updateCameraClock, 1000)
  }

  function setGeotagStatus(message, active = false) {
    $('#camera-location').textContent = message
    const state = $('#geotag-state')
    state.textContent = `⌖ ${message}`
    state.classList.toggle('active', active)
  }

  function requestGeotag() {
    currentLocation = null
    if (!navigator.geolocation) {
      setGeotagStatus('Location unavailable')
      return
    }
    setGeotagStatus('Locating…')
    navigator.geolocation.getCurrentPosition(
      (position) => {
        currentLocation = {
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy: position.coords.accuracy,
          recordedAt: new Date(position.timestamp).toISOString(),
        }
        setGeotagStatus(
          `${currentLocation.latitude.toFixed(5)}, ${currentLocation.longitude.toFixed(5)} ±${Math.round(currentLocation.accuracy)}m`,
          true,
        )
      },
      (error) => {
        const message = error.code === error.PERMISSION_DENIED ? 'Location permission denied' : 'Location unavailable'
        setGeotagStatus(message)
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 },
    )
  }

  function resetTorch() {
    torchTrack = null
    torchEnabled = false
    torchButton.disabled = true
    torchButton.classList.remove('active')
    torchButton.setAttribute('aria-pressed', 'false')
    torchButton.querySelector('small').textContent = 'Flash unavailable'
  }

  function configureCameraControls() {
    const [track] = stream?.getVideoTracks?.() || []
    const settings = track?.getSettings?.() || {}
    $('#camera-resolution').textContent = settings.height ? `${settings.height}p` : 'Camera'
    $('#camera-fps').textContent = settings.frameRate ? `${Math.round(settings.frameRate)} FPS` : '-- FPS'
    let capabilities = {}
    try { capabilities = track?.getCapabilities?.() || {} } catch { capabilities = {} }
    if (track && capabilities.torch === true) {
      torchTrack = track
      torchButton.disabled = false
      torchButton.querySelector('small').textContent = 'Flash off'
    } else {
      resetTorch()
    }
  }

  async function toggleTorch() {
    if (!torchTrack) return
    const next = !torchEnabled
    try {
      await torchTrack.applyConstraints({ advanced: [{ torch: next }] })
      torchEnabled = next
      torchButton.classList.toggle('active', next)
      torchButton.setAttribute('aria-pressed', String(next))
      torchButton.querySelector('small').textContent = next ? 'Flash on' : 'Flash off'
    } catch {
      resetTorch()
      showToast('Flash control is not supported by this camera or browser.', true)
    }
  }

  function stopScanner() {
    window.clearTimeout(scanTimer)
    scanTimer = null
    scanAttempts = 0
    scannerFailures = 0
    recordingAbsentFrames = 0
    recordingAbsentSince = 0
    recordingCodeSeenInCycle = false
  }

  function stopStream() {
    stopScanner()
    if (stream) stream.getTracks().forEach((track) => track.stop())
    stream = null
    preview.srcObject = null
    window.clearInterval(recordTimer)
    recordTimer = null
    window.clearInterval(clockTimer)
    clockTimer = null
    resetTorch()
  }

  function showStep(step) {
    codeStep.hidden = step !== 'code'
    cameraStep.hidden = step !== 'camera'
    reviewStep.hidden = step !== 'review'
    modal.querySelector('.capture-modal').classList.toggle('camera-active', step === 'camera')
    if (step === 'camera') startCameraClock()
    else {
      window.clearInterval(clockTimer)
      clockTimer = null
    }
  }

  function showManualEntry(message = '') {
    stopStream()
    showStep('code')
    if (message) showToast(message, true)
    window.setTimeout(() => orderInput.focus(), 0)
  }

  function openModal(type) {
    evidenceType = type
    $('#capture-type').textContent = type
    $('#capture-type').classList.toggle('rto', type === 'RTO')
    $('#capture-title').textContent = type === 'RTO' ? 'Record an RTO' : 'Pack an order'
    orderInput.value = ''
    currentLocation = null
    setGeotagStatus('Location pending')
    openCameraButton.disabled = true
    modal.hidden = false
    void openCamera(false)
  }

  function closeModal() {
    if (recorder?.state === 'recording') stopRecording()
    stopStream()
    if (reviewVideo.src) URL.revokeObjectURL(reviewVideo.src)
    recordedBlob = null
    modal.hidden = true
  }

  async function createBarcodeDetector() {
    const desired = ['qr_code', 'code_128', 'code_39', 'code_93', 'ean_13', 'ean_8', 'itf', 'upc_a', 'upc_e', 'codabar', 'data_matrix', 'aztec', 'pdf417']
    if ('BarcodeDetector' in window) {
      try {
        const supported = await window.BarcodeDetector.getSupportedFormats()
        const formats = desired.filter((format) => supported.includes(format))
        if (formats.length) return { kind: 'native', reader: new window.BarcodeDetector({ formats }) }
      } catch {
        // Fall through to the bundled ZXing reader.
      }
    }
    if (window.ZXingBrowser?.BrowserMultiFormatReader) {
      return {
        kind: 'zxing',
        reader: new window.ZXingBrowser.BrowserMultiFormatReader(undefined, {
          delayBetweenScanAttempts: 140,
          delayBetweenScanSuccess: 350,
        }),
      }
    }
    return null
  }

  function acceptTrackingCode(rawValue) {
    const value = extractTrackingCode(rawValue)
    if (value.length < 3 || value.length > 64) return false
    orderInput.value = value
    $('#camera-order-code').textContent = value
    recordState.textContent = 'CODE DETECTED'
    scanFeedback.classList.add('found')
    scanFeedback.innerHTML = `<span></span> Tracking number detected: <strong>${value}</strong>`
    recordButton.disabled = false
    stopScanner()
    window.setTimeout(() => {
      if (stream && recorder?.state !== 'recording' && !cameraStep.hidden) startRecording()
    }, 150)
    return true
  }

  function registerDetection(rawValue) {
    const candidate = extractTrackingCode(rawValue)
    if (!candidate) {
      if (recorder?.state !== 'recording') {
        scanFeedback.innerHTML = '<span></span> Code detected, but no AWB was found in its data — keep scanning or enter it manually'
      }
      return false
    }
    if (recorder?.state === 'recording') {
      if (!sameRecordingCode(candidate, normalizedCode())) {
        scanFeedback.innerHTML = `<span></span> Different barcode ignored: ${candidate}`
        return false
      }
      recordingCodeSeenInCycle = true
      recordingAbsentFrames = 0
      recordingAbsentSince = 0
      if (!recordingStopArmed) return false
      scanFeedback.innerHTML = '<span></span> Same AWB detected — stopping and saving recording'
      autoSaveAfterStop = true
      stopReason = 'SAME_AWB_RESCAN'
      stopRecording()
      return true
    }
    return acceptTrackingCode(candidate)
  }

  function prioritizeRecordingCode(rawValues) {
    const values = rawValues.filter(Boolean)
    if (recorder?.state !== 'recording') return values
    const activeCode = normalizedCode()
    return values.sort((left, right) => {
      const leftMatches = sameRecordingCode(extractTrackingCode(left), activeCode) ? 1 : 0
      const rightMatches = sameRecordingCode(extractTrackingCode(right), activeCode) ? 1 : 0
      return rightMatches - leftMatches
    })
  }

  function registerDetections(rawValues) {
    const values = prioritizeRecordingCode(rawValues)
    if (recorder?.state === 'recording') {
      const activeCode = normalizedCode()
      const matchingValue = values.find((value) => sameRecordingCode(extractTrackingCode(value), activeCode))
      if (matchingValue) return registerDetection(matchingValue)
      if (values.length) registerDetection(values[0])
      return false
    }
    for (const value of values) if (registerDetection(value)) return true
    return false
  }

  function drawScanCanvas() {
    const width = Math.min(960, preview.videoWidth || 960)
    const height = Math.round(width * (preview.videoHeight || 540) / (preview.videoWidth || 960))
    scanCanvas.width = width
    scanCanvas.height = height
    const context = scanCanvas.getContext('2d', { willReadFrequently: true })
    context.drawImage(preview, 0, 0, width, height)
  }

  async function scanOnServer(requireRepeat = true, recordingCodeAtRequest = '') {
    if (serverScanInFlight) return false
    serverScanInFlight = true
    try {
      const blob = await new Promise((resolve) => scanCanvas.toBlob(resolve, 'image/jpeg', 0.84))
      if (!blob) return false
      const form = new FormData()
      form.append('frame', blob, 'camera-frame.jpg')
      const response = await fetch('/api/scan', { method: 'POST', headers: csrfHeaders(), body: form })
      if (!response.ok) {
        scannerFailures += 1
        return false
      }
      const data = await response.json()
      if (recordingCodeAtRequest && (
        recorder?.state !== 'recording'
        || !sameRecordingCode(recordingCodeAtRequest, normalizedCode())
      )) return false
      const values = (data.codes || []).map((code) => code.text)
      if (requireRepeat ? registerDetections(values) : values.some((value) => acceptTrackingCode(value))) return true
      return false
    } catch {
      scannerFailures += 1
      return false
    } finally {
      serverScanInFlight = false
    }
  }

  async function scanFrame() {
    if (!stream || cameraStep.hidden) return
    recordingCodeSeenInCycle = false
    try {
      drawScanCanvas()
      let rawValues = []
      if (detector?.kind === 'native') {
        const results = await detector.reader.detect(preview)
        rawValues = results.map((result) => result.rawValue || '')
      } else if (detector?.kind === 'zxing') {
        // ZXing browser releases have exposed both synchronous and Promise results.
        const result = await Promise.resolve(detector.reader.decodeFromCanvas(scanCanvas))
        rawValues = [result.getText?.() || result.text || '']
      }
      if (registerDetections(rawValues)) return
    } catch {
      // A transient unreadable frame is expected while the camera is moving.
    }
    scanAttempts += 1
    // Keep the Python fallback active even when the browser decoder is missing.
    const isRecording = recorder?.state === 'recording'
    const serverInterval = isRecording ? 2 : detector ? 3 : 1
    if (scanAttempts % serverInterval === 0) {
      if (isRecording) {
        // Do not pause browser decoding while a slower server fallback is running.
        // A matching response can still stop the active recording asynchronously.
        void scanOnServer(true, normalizedCode())
      } else if (await scanOnServer()) return
    }
    if (recorder?.state === 'recording') {
      const now = performance.now()
      if (recordingCodeSeenInCycle) {
        recordingAbsentFrames = 0
        recordingAbsentSince = 0
      } else {
        recordingAbsentFrames += 1
        if (!recordingAbsentSince) recordingAbsentSince = now
      }
      const recordingOldEnough = now - recordingStartedAt >= 2000
      const labelWasAwayLongEnough = recordingAbsentSince && now - recordingAbsentSince >= 650
      if (!recordingStopArmed && recordingOldEnough && labelWasAwayLongEnough && recordingAbsentFrames >= 2) {
        recordingStopArmed = true
        scanFeedback.innerHTML = `<span></span> Recording linked to ${normalizedCode()} — scan the same AWB again to stop`
      }
    }
    if (scannerFailures >= 3) {
      scanFeedback.innerHTML = '<span></span> Scanner service is unavailable — use a label photo or enter the AWB manually'
    }
    scanTimer = window.setTimeout(scanFrame, 220)
  }

  async function scanLabelPhoto(file) {
    if (!file?.type?.startsWith('image/')) {
      showToast('Choose a JPG, PNG, or other label image.', true)
      return
    }
    scanFeedback.innerHTML = '<span></span> Reading barcode from label photo…'
    const objectUrl = URL.createObjectURL(file)
    try {
      const image = new Image()
      image.src = objectUrl
      await image.decode()
      const maxSide = 2200
      const scale = Math.min(1, maxSide / Math.max(image.naturalWidth, image.naturalHeight))
      scanCanvas.width = Math.max(1, Math.round(image.naturalWidth * scale))
      scanCanvas.height = Math.max(1, Math.round(image.naturalHeight * scale))
      scanCanvas.getContext('2d', { willReadFrequently: true }).drawImage(image, 0, 0, scanCanvas.width, scanCanvas.height)

      if (detector?.kind === 'native') {
        const results = await detector.reader.detect(scanCanvas)
        for (const result of results) if (acceptTrackingCode(result.rawValue)) return
      } else if (detector?.kind === 'zxing') {
        try {
          const result = await Promise.resolve(detector.reader.decodeFromCanvas(scanCanvas))
          if (acceptTrackingCode(result.getText?.() || result.text || '')) return
        } catch {
          // The Python decoder below is more tolerant of shipping-label photos.
        }
      }
      if (!await scanOnServer(false)) {
        scanFeedback.innerHTML = '<span></span> No readable code found — move closer, avoid glare, and keep the entire barcode in frame'
      }
    } catch {
      showToast('That label photo could not be read.', true)
    } finally {
      URL.revokeObjectURL(objectUrl)
    }
  }

  async function openCamera(autoRecord) {
    showStep('camera')
    const hasManualCode = normalizedCode().length >= 3
    $('#camera-order-code').textContent = hasManualCode ? normalizedCode() : 'SCAN AWB / TRACKING LABEL'
    recordState.textContent = hasManualCode ? 'CODE READY' : 'SCANNING'
    recordState.classList.remove('live')
    scanFeedback.classList.remove('found')
    scanFeedback.innerHTML = '<span></span> Reading QR and barcode — hold the label steady inside the frame'
    cameraMessage.textContent = 'Starting camera…'
    cameraMessage.hidden = false
    recordButton.disabled = true
    try {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') throw new Error('Camera recording is not supported in this browser.')
      const video = { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video, audio: true })
      } catch {
        // A denied/unavailable microphone must not disable barcode scanning.
        stream = await navigator.mediaDevices.getUserMedia({ video, audio: false })
      }
      preview.srcObject = stream
      await preview.play()
      configureCameraControls()
      requestGeotag()
      cameraMessage.hidden = true
      if (hasManualCode) {
        recordButton.disabled = false
        scanFeedback.classList.add('found')
        scanFeedback.innerHTML = `<span></span> Tracking number confirmed: <strong>${normalizedCode()}</strong>`
        if (autoRecord) window.setTimeout(startRecording, 700)
      } else {
        detector = await createBarcodeDetector()
        void scanFrame()
      }
    } catch (error) {
      cameraMessage.textContent = `${error.message || 'Camera access was blocked.'} Allow camera access, scan a label photo, or enter the AWB manually.`
    }
  }

  function startRecording() {
    if (!stream || normalizedCode().length < 3 || recorder?.state === 'recording') return
    stopScanner()
    chunks = []
    elapsed = 0
    cameraElapsed.textContent = '00:00'
    const efficientMimeTypes = [
      'video/webm;codecs=vp8,opus',
      'video/webm;codecs=vp8',
      'video/webm',
      'video/mp4',
    ]
    const efficientMimeType = efficientMimeTypes.find((type) => MediaRecorder.isTypeSupported?.(type))
    const options = efficientMimeType
      ? { mimeType: efficientMimeType, videoBitsPerSecond: 650_000, audioBitsPerSecond: 48_000 }
      : undefined
    try {
      recorder = new MediaRecorder(stream, options)
    } catch {
      // Let the browser choose its most efficient native encoder if options differ.
      recorder = new MediaRecorder(stream)
    }
    recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data) }
    recorder.onstop = () => {
      announceRecordingEnd()
      recordedBlob = new Blob(chunks, { type: recorder.mimeType || 'video/webm' })
      stopStream()
      reviewVideo.src = URL.createObjectURL(recordedBlob)
      $('#review-code').textContent = normalizedCode()
      $('#review-duration').textContent = formatTime(elapsed)
      $('#review-size').textContent = `${(recordedBlob.size / 1024 / 1024).toFixed(1)} MB`
      showStep('review')
      if (autoSaveAfterStop) {
        $('#save-video').textContent = 'Saving evidence…'
        void saveEvidence()
      }
    }
    recorder.start(1000)
    recordingEndAnnounced = false
    announceRecordingStatus('Recording Started')
    autoSaveAfterStop = false
    stopReason = 'MANUAL'
    recordingSessionId = newSessionId()
    void fetch('/api/recording-sessions', {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        id: recordingSessionId,
        order_code: normalizedCode(),
        evidence_type: evidenceType,
        latitude: currentLocation?.latitude ?? null,
        longitude: currentLocation?.longitude ?? null,
        location_accuracy_m: currentLocation?.accuracy ?? null,
        location_recorded_at_utc: currentLocation?.recordedAt ?? null,
      }),
    }).catch(() => {})
    recordingStartedAt = performance.now()
    recordingStopArmed = false
    recordingAbsentFrames = 0
    recordingAbsentSince = 0
    recordButton.disabled = false
    recordButton.classList.add('recording')
    recordButton.setAttribute('aria-label', 'Stop recording')
    recordState.classList.add('live')
    recordState.textContent = '● REC 00:00'
    scanFeedback.innerHTML = `<span></span> AWB ${normalizedCode()} linked — move the label away, then scan it again to stop`
    recordTimer = window.setInterval(() => {
      elapsed += 1
      cameraElapsed.textContent = formatTime(elapsed)
      recordState.textContent = `● REC ${formatTime(elapsed)}`
    }, 1000)
    scanTimer = window.setTimeout(scanFrame, 300)
  }

  function stopRecording() {
    if (recorder?.state === 'recording') {
      recorder.stop()
      announceRecordingEnd()
    }
    recordButton.classList.remove('recording')
    recordButton.setAttribute('aria-label', 'Start recording')
    recordState.classList.remove('live')
  }

  function uploadChunk(uploadUrl, blob, start, onProgress) {
    const chunkSize = 8 * 1024 * 1024
    const endExclusive = Math.min(start + chunkSize, blob.size)
    const chunk = blob.slice(start, endExclusive, blob.type)
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('PUT', uploadUrl)
      xhr.setRequestHeader('Content-Type', blob.type || 'application/octet-stream')
      xhr.setRequestHeader('Content-Range', `bytes ${start}-${endExclusive - 1}/${blob.size}`)
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(Math.min(99, ((start + event.loaded) / blob.size) * 100))
      }
      xhr.onload = () => {
        if (xhr.status === 308) {
          const confirmed = xhr.getResponseHeader('Range')?.match(/bytes=0-(\d+)/i)
          const nextOffset = confirmed ? Number(confirmed[1]) + 1 : endExclusive
          return resolve({ nextOffset, driveFile: null, uploadUrl: xhr.getResponseHeader('Location') || uploadUrl })
        }
        if (xhr.status === 200 || xhr.status === 201) {
          try {
            return resolve({ nextOffset: blob.size, driveFile: JSON.parse(xhr.responseText || '{}'), uploadUrl })
          } catch {
            return reject(new Error('Google Drive finished the upload but returned an unreadable response.'))
          }
        }
        reject(new Error(`Google Drive rejected the upload (response ${xhr.status || 'network error'}).`))
      }
      xhr.onerror = () => reject(new Error('The network was interrupted while uploading to Google Drive.'))
      xhr.send(chunk)
    })
  }

  function queryUploadStatus(uploadUrl, blobSize) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('PUT', uploadUrl)
      xhr.setRequestHeader('Content-Range', `bytes */${blobSize}`)
      xhr.onload = () => {
        if (xhr.status === 308) {
          const confirmed = xhr.getResponseHeader('Range')?.match(/bytes=0-(\d+)/i)
          return resolve({ nextOffset: confirmed ? Number(confirmed[1]) + 1 : 0, driveFile: null })
        }
        if (xhr.status === 200 || xhr.status === 201) {
          try { return resolve({ nextOffset: blobSize, driveFile: JSON.parse(xhr.responseText || '{}') }) } catch {
            return reject(new Error('Google Drive returned an unreadable upload status.'))
          }
        }
        if (xhr.status === 404 || xhr.status === 410) return resolve({ expired: true })
        reject(new Error(`Google Drive could not resume the upload (response ${xhr.status || 'network error'}).`))
      }
      xhr.onerror = () => reject(new Error('The network was interrupted while checking Google Drive.'))
      xhr.send()
    })
  }

  async function processDriveUpload(upload, onProgress = () => {}) {
    let working = upload
    let driveFile = null
    if (working.uploadUrl) {
      onProgress(1, 'Checking the interrupted Google Drive upload…')
      const status = await queryUploadStatus(working.uploadUrl, working.blob.size)
      if (status.expired) {
        working = { ...working, uploadUrl: '', uploadedBytes: 0 }
        Object.assign(upload, working)
        await persistQueuedUpload(working)
      } else {
        working = { ...working, uploadedBytes: status.nextOffset }
        driveFile = status.driveFile
        Object.assign(upload, working)
        await persistQueuedUpload(working)
      }
    }
    if (!working.uploadUrl) {
      onProgress(1, 'Creating a secure Google Drive upload…')
      const response = await fetch('/api/drive/uploads/initiate', {
        method: 'POST',
        headers: csrfHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          upload_id: working.uploadId,
          order_code: working.orderCode,
          evidence_type: working.evidenceType,
          duration_seconds: working.durationSeconds,
          size_bytes: working.blob.size,
          sha256: working.sha256,
          mime_type: working.blob.type || 'video/webm',
          recording_session_id: working.recordingSessionId,
          stop_reason: working.stopReason,
          latitude: working.location?.latitude ?? null,
          longitude: working.location?.longitude ?? null,
          location_accuracy_m: working.location?.accuracy ?? null,
          location_recorded_at_utc: working.location?.recordedAt ?? null,
        }),
      })
      const initiated = await readJsonResponse(response)
      working = { ...working, uploadUrl: initiated.upload_url, uploadedBytes: 0 }
      Object.assign(upload, working)
      await persistQueuedUpload(working)
    }

    let offset = Number(working.uploadedBytes || 0)
    while (offset < working.blob.size) {
      const result = await uploadChunk(working.uploadUrl, working.blob, offset, (percent) => {
        onProgress(percent, `Uploading directly to Google Drive… ${Math.round(percent)}%`)
      })
      offset = result.nextOffset
      driveFile = result.driveFile || driveFile
      working = { ...working, uploadedBytes: offset, uploadUrl: result.uploadUrl || working.uploadUrl }
      Object.assign(upload, working)
      await persistQueuedUpload(working)
    }
    if (!driveFile?.id) throw new Error('Google Drive did not confirm the uploaded file.')

    onProgress(100, 'Verifying the saved Drive file…')
    const completion = await fetch('/api/drive/uploads/complete', {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ upload_id: working.uploadId, drive_file_id: driveFile.id }),
    })
    const result = await readJsonResponse(completion)
    try { await removeQueuedUpload(working.uploadId) } catch { /* Upload is already verified remotely. */ }
    return result
  }

  function setUploadProgress(percent, message) {
    const progress = $('#upload-progress')
    progress.querySelector('i').style.width = `${Math.max(2, Math.min(100, percent))}%`
    progress.querySelector('span').textContent = message
  }

  async function saveEvidence() {
    if (!recordedBlob) return
    const button = $('#save-video')
    const progress = $('#upload-progress')
    button.disabled = true
    progress.hidden = false
    try {
      let result
      if (driveConnected) {
        setUploadProgress(1, 'Preparing recording for Google Drive…')
        const upload = activeDriveUpload || {
          uploadId: newSessionId(),
          ownerSub: currentUserSub,
          orderCode: normalizedCode(),
          evidenceType,
          durationSeconds: elapsed,
          recordingSessionId,
          stopReason,
          location: currentLocation,
          blob: recordedBlob,
          sha256: await sha256Hex(recordedBlob),
          uploadUrl: '',
          uploadedBytes: 0,
          createdAt: new Date().toISOString(),
        }
        activeDriveUpload = upload
        if (!upload.uploadUrl && !upload.uploadedBytes && !await persistQueuedUpload(upload)) {
          showToast('Offline retry storage is unavailable; keep this page open until Drive finishes.', true)
        }
        result = await processDriveUpload(upload, setUploadProgress)
        activeDriveUpload = null
      } else {
        const form = new FormData()
        form.append('order_code', normalizedCode())
        form.append('evidence_type', evidenceType)
        form.append('duration_seconds', String(elapsed))
        form.append('recording_session_id', recordingSessionId)
        form.append('stop_reason', stopReason)
        form.append('latitude', currentLocation?.latitude ?? '')
        form.append('longitude', currentLocation?.longitude ?? '')
        form.append('location_accuracy_m', currentLocation?.accuracy ?? '')
        form.append('location_recorded_at_utc', currentLocation?.recordedAt ?? '')
        form.append('video', recordedBlob, `evidence.${recordedBlob.type.includes('webm') ? 'webm' : 'mp4'}`)
        const response = await fetch('/api/evidence', { method: 'POST', headers: csrfHeaders(), body: form })
        const responseText = await response.text()
        try { result = responseText ? JSON.parse(responseText) : {} } catch { result = {} }
        if (!response.ok) {
          if (response.status === 413 || /FUNCTION_PAYLOAD_TOO_LARGE/i.test(responseText)) {
            throw new Error('This recording is too large for the current Vercel upload limit. Connect Google Drive, then try again.')
          }
          throw new Error(result.error || `Evidence could not be saved (server response ${response.status}).`)
        }
      }
      if (!result.redirect || typeof result.redirect !== 'string') {
        throw new Error('Evidence was received, but the server did not return a valid library link.')
      }
      const duplicateMessage = result.duplicate_count > 1 ? ` Duplicate AWB: ${result.duplicate_count} recordings found.` : ''
      showToast(`Evidence saved with tracking number ${normalizedCode()}.${duplicateMessage}`)
      window.setTimeout(() => { window.location.assign(result.redirect) }, result.duplicate_count > 1 ? 1400 : 450)
    } catch (error) {
      showToast(error.message || 'Evidence could not be saved.', true)
      button.textContent = '✓ Save evidence'
      button.disabled = false
      setUploadProgress(2, driveConnected ? 'Upload paused — use Uploads to retry safely.' : 'Saving failed — try again.')
    }
  }

  async function renderDeviceUploadQueue() {
    const container = $('#device-upload-list')
    if (!container) return
    let uploads = []
    try { uploads = (await listQueuedUploads()).filter((upload) => upload.ownerSub === currentUserSub) } catch {
      container.innerHTML = '<p class="settings-note">This browser could not open its offline upload queue.</p>'
      return
    }
    if (!uploads.length) {
      container.innerHTML = '<div class="empty-state compact"><span>✓</span><h3>Device retry queue is clear</h3><p>No interrupted browser uploads are waiting.</p></div>'
      return
    }
    container.innerHTML = ''
    for (const upload of uploads.sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)))) {
      const row = document.createElement('article')
      row.className = 'queue-row device-queue-row'
      row.innerHTML = '<span class="file-icon">▶</span><div><strong></strong><small></small><span class="progress"><i></i></span></div><span class="status pending">Waiting</span><button class="secondary-button">Retry</button>'
      row.querySelector('strong').textContent = upload.orderCode
      row.querySelector('small').textContent = `${upload.evidenceType} · ${(upload.blob.size / 1024 / 1024).toFixed(1)} MB`
      const status = row.querySelector('.status')
      const bar = row.querySelector('.progress i')
      const retry = row.querySelector('button')
      retry.disabled = !driveConnected
      retry.addEventListener('click', async () => {
        retry.disabled = true
        try {
          const result = await processDriveUpload(upload, (percent, message) => {
            bar.style.width = `${percent}%`
            status.textContent = message
          })
          status.textContent = 'Verified'
          window.setTimeout(() => window.location.assign(result.redirect), 350)
        } catch (error) {
          status.textContent = 'Retry needed'
          retry.disabled = false
          showToast(error.message || 'Upload could not be resumed.', true)
        }
      })
      container.appendChild(row)
    }
  }

  $$('.action-card[data-record-type], [data-record-type]').forEach((button) => button.addEventListener('click', () => openModal(button.dataset.recordType)))
  orderInput.addEventListener('input', () => { openCameraButton.disabled = normalizedCode().length < 3 })
  orderInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && normalizedCode().length >= 3) {
      event.preventDefault()
      void openCamera(true)
    }
  })
  openCameraButton.addEventListener('click', () => void openCamera(true))
  recordButton.addEventListener('click', () => recorder?.state === 'recording' ? stopRecording() : startRecording())
  torchButton.addEventListener('click', () => void toggleTorch())
  $('#close-capture').addEventListener('click', closeModal)
  $('#back-to-code').addEventListener('click', () => showManualEntry())
  $('#scan-image-button').addEventListener('click', () => $('#scan-image-input').click())
  $('#scan-image-input').addEventListener('change', (event) => {
    const [file] = event.target.files || []
    if (file) void scanLabelPhoto(file)
    event.target.value = ''
  })
  $('#retake').addEventListener('click', () => { if (reviewVideo.src) URL.revokeObjectURL(reviewVideo.src); recordedBlob = null; void openCamera(true) })
  $('#save-video').addEventListener('click', saveEvidence)
  modal.addEventListener('click', (event) => { if (event.target === modal) closeModal() })

  const sidebar = $('#sidebar')
  const scrim = $('#nav-scrim')
  const logoutForm = $('#logout-form')
  logoutForm?.addEventListener('submit', async (event) => {
    event.preventDefault()
    try {
      const uploads = await listQueuedUploads()
      await Promise.all(uploads.filter((upload) => upload.ownerSub === currentUserSub).map((upload) => removeQueuedUpload(upload.uploadId)))
    } catch {
      // Signing out must still work when browser storage is unavailable.
    }
    logoutForm.submit()
  })
  $('#menu-button').addEventListener('click', () => { sidebar.classList.add('open'); scrim.classList.add('show') })
  scrim.addEventListener('click', () => { sidebar.classList.remove('open'); scrim.classList.remove('show') })
  document.addEventListener('keydown', (event) => {
    const target = event.target
    const isEditable = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || target?.isContentEditable
    if (!modal.hidden && !cameraStep.hidden && !isEditable && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const now = performance.now()
      if (now - keyboardScanLastAt > 120) keyboardScanBuffer = ''
      keyboardScanLastAt = now
      if (event.key === 'Enter') {
        const scanned = keyboardScanBuffer
        keyboardScanBuffer = ''
        if (scanned.length >= 3) {
          event.preventDefault()
          registerDetection(scanned)
          return
        }
      } else if (event.key.length === 1) {
        keyboardScanBuffer += event.key
      }
    }
    if (event.key === 'Escape' && !modal.hidden) closeModal()
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); window.location.href = '/evidence' }
  })
  $$('[data-safe-cleanup]').forEach((button) => button.addEventListener('click', () => showToast('No files are eligible: remote verification is not configured')))
  void renderDeviceUploadQueue()
})()
