// Lee el detalle real del error que manda el backend (FastAPI responde {"detail": "..."})
// en vez de mostrar siempre un mensaje generico que oculta la causa real.
async function extractErrorDetail(response, fallbackMessage) {
    try {
        const data = await response.json();
        if (data && data.detail) return data.detail;
    } catch (_) {
        // El cuerpo no era JSON (p. ej. un 500 sin manejar) -- nos quedamos con el fallback
    }
    return fallbackMessage;
}

document.addEventListener('DOMContentLoaded', () => {
    // Elements
    const excelDropzone = document.getElementById('excel_dropzone');
    const pdfDropzone = document.getElementById('pdf_dropzone');
    const excelFileInput = document.getElementById('excel_file');
    const pdfFileInput = document.getElementById('pdf_file');
    const excelFileName = document.getElementById('excel_file_name');
    const pdfFileName = document.getElementById('pdf_file_name');

    const excelPreviewSection = document.getElementById('excel_preview_section');
    const rowDetectedBadge = document.getElementById('row_detected_badge');
    const rowSelector = document.getElementById('row_selector');
    const keyColSelector = document.getElementById('key_col_selector');
    const compareColsSelector = document.getElementById('compare_cols_selector');
    const excelPreviewTable = document.getElementById('excel_preview_table');
    
    const pdfDpiInput = document.getElementById('pdf_dpi');
    const dpiVal = document.getElementById('dpi_val');
    const imgFilterInput = document.getElementById('img_filter');
    const similarityThresholdInput = document.getElementById('similarity_threshold');
    const similarityVal = document.getElementById('similarity_val');
    const startPageInput = document.getElementById('start_page');
    const endPageInput = document.getElementById('end_page');
    const totalPagesHint = document.getElementById('total_pages_hint');
    
    const actionPanel = document.getElementById('action_panel');
    const startAuditBtn = document.getElementById('start_audit_btn');
    const estimacionTiempo = document.getElementById('estimacion_tiempo');
    const estimacionTiempoTexto = document.getElementById('estimacion_tiempo_texto');
    const etaRestante = document.getElementById('eta_restante');
    
    const progressSection = document.getElementById('progress_section');
    const progressStatus = document.getElementById('progress_status');
    const progressPercent = document.getElementById('progress_percent');
    const progressBar = document.getElementById('progress_bar');
    const progressSubtext = document.getElementById('progress_subtext');
    
    const resultsSection = document.getElementById('results_section');
    const metricCorrect = document.getElementById('metric_correct');
    const metricAlerts = document.getElementById('metric_alerts');
    const metricMissing = document.getElementById('metric_missing');
    const metricHuerfanos = document.getElementById('metric_huerfanos');
    const downloadReportBtn = document.getElementById('download_report_btn');
    const elapsedTimerEl = document.getElementById('elapsed_timer');
    const elapsedFinalEl = document.getElementById('elapsed_final');

    // State variables
    let excelFileObj = null;
    let pdfFileObj = null;
    let excelHeaders = [];
    let excelPreviews = [];
    let currentTaskId = null;
    let pollInterval = null;
    let localPdfBlobUrl = null;
    let lastViewedPage = null;
    let selectedRawTextPage = null;
    let userSelectedCardManually = false;
    let clientTimerInterval = null;
    let clientTimerStartedAt = null;
    let editingDocument = null;
    let lastLiveResults = [];
    let excelRecordsCache = null;
    let currentSource = 'pdf';
    let cardSearchQuery = '';
    let onlySuggestions = false;
    // Segundos por pagina medidos en corridas anteriores de ESTA maquina (lo manda
    // /api/pdf-info). null = todavia no hay historial.
    let segundosPorPagina = null;

    // Range input listeners
    pdfDpiInput.addEventListener('input', (e) => { dpiVal.textContent = e.target.value; });
    similarityThresholdInput.addEventListener('input', (e) => { similarityVal.textContent = e.target.value; });

    // Helper generico para modales (abrir/cerrar con boton X, click afuera, o Escape) --
    // usado tanto por el de Configuracion como por el de texto OCR crudo, para no
    // duplicar la misma logica de apertura/cierre dos veces.
    function setupModal(overlayEl, closeBtnEl) {
        function open() { overlayEl.classList.remove('hidden'); }
        function close() { overlayEl.classList.add('hidden'); }
        if (closeBtnEl) closeBtnEl.addEventListener('click', close);
        overlayEl.addEventListener('click', (e) => {
            if (e.target === overlayEl) close();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !overlayEl.classList.contains('hidden')) close();
        });
        return { open, close };
    }

    // Config modal (se abre con el engranaje del header)
    const openConfigBtn = document.getElementById('open_config_btn');
    const configModal = setupModal(
        document.getElementById('config_modal_overlay'),
        document.getElementById('close_config_btn')
    );
    openConfigBtn.addEventListener('click', configModal.open);

    // Modal de texto OCR crudo (se abre desde el boton de cada tarjeta en la Consola)
    const rawTextModal = setupModal(
        document.getElementById('raw_text_modal_overlay'),
        document.getElementById('close_raw_text_btn')
    );

    function openRawTextModal(res) {
        const subtitle = document.getElementById('raw_text_modal_subtitle');
        const content = document.getElementById('raw_text_modal_content');
        if (subtitle) {
            subtitle.textContent = `${res.document || 'Sin documento'} (Págs. ${(res.pages || []).join(', ')})`;
        }
        if (content) {
            content.textContent = res.raw_text && res.raw_text.trim()
                ? res.raw_text
                : 'No se capturó texto OCR para esta tarjeta.';
        }
        rawTextModal.open();
    }

    // Modo claro / oscuro -- el tema ya se aplico antes del primer render (ver script
    // inline en el <head>), aqui solo hace falta sincronizar el icono/texto del boton
    // y manejar el click para alternar y guardar la preferencia.
    const themeToggleBtn = document.getElementById('theme_toggle_btn');
    const themeToggleIcon = document.getElementById('theme_toggle_icon');

    function syncThemeButton() {
        const esClaro = document.documentElement.getAttribute('data-theme') === 'light';
        if (themeToggleIcon) {
            themeToggleIcon.innerHTML = `<use href="#${esClaro ? 'icon-sun' : 'icon-moon'}"/>`;
        }
        if (themeToggleBtn) {
            themeToggleBtn.title = esClaro ? 'Cambiar a modo oscuro' : 'Cambiar a modo claro';
        }
    }
    syncThemeButton();

    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', () => {
            const esClaroAhora = document.documentElement.getAttribute('data-theme') === 'light';
            if (esClaroAhora) {
                document.documentElement.removeAttribute('data-theme');
                try { localStorage.setItem('tema', 'dark'); } catch (e) {}
            } else {
                document.documentElement.setAttribute('data-theme', 'light');
                try { localStorage.setItem('tema', 'light'); } catch (e) {}
            }
            syncThemeButton();
        });
    }

    // Drag and Drop implementation
    setupDropzone(excelDropzone, excelFileInput, handleExcelSelect);
    setupDropzone(pdfDropzone, pdfFileInput, handlePdfSelect);

    function setupDropzone(dropzone, input, handler) {
        dropzone.addEventListener('click', () => input.click());
        
        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('active');
        });

        dropzone.addEventListener('dragleave', () => {
            dropzone.classList.remove('active');
        });

        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('active');
            if (e.dataTransfer.files.length > 0) {
                input.files = e.dataTransfer.files;
                handler(e.dataTransfer.files[0]);
            }
        });

        input.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handler(e.target.files[0]);
            }
        });
    }

    // Handle excel select
    async function handleExcelSelect(file) {
        excelFileObj = file;
        excelFileName.textContent = file.name;
        
        // Call backend to analyze Excel
        const formData = new FormData();
        formData.append('excel_file', file);
        
        try {
            excelFileName.textContent = 'Analizando archivo...';
            const response = await fetch('/api/analyze-excel', {
                method: 'POST',
                body: formData
            });
            
            if (!response.ok) {
                throw new Error(await extractErrorDetail(response, 'Error al analizar archivo Excel'));
            }

            const data = await response.json();
            excelHeaders = data.headers;
            excelPreviews = data.preview;
            
            rowDetectedBadge.textContent = `Fila de encabezado detectada: Fila ${data.detected_row_idx + 1}`;
            
            // Populating headers & selectors
            populateSelectors(data.row_previews, data.detected_row_idx, data.headers, data.key_col_default, data.compare_cols_default);
            renderPreviewTable(data.preview, data.headers);
            
            // excelPreviewSection.classList.remove('hidden'); (oculto por ahora)
            excelFileName.textContent = `${file.name} (Analizado)`;
            checkReadyToAudit();
        } catch (err) {
            console.error(err);
            excelFileName.textContent = 'Error al procesar archivo';
            alert('Error al procesar el Excel: ' + err.message);
        }
    }

    // Handle PDF select
    async function handlePdfSelect(file) {
        pdfFileObj = file;
        pdfFileName.textContent = file.name;
        
        const formData = new FormData();
        formData.append('pdf_file', file);
        
        try {
            pdfFileName.textContent = 'Contando páginas...';
            const response = await fetch('/api/pdf-info', {
                method: 'POST',
                body: formData
            });
            if (!response.ok) {
                throw new Error(await extractErrorDetail(response, 'Error al analizar el PDF'));
            }
            const data = await response.json();

            pdfFileName.textContent = `${file.name} (${data.total_pages} págs.)`;
            startPageInput.value = 1;
            startPageInput.max = data.total_pages;
            endPageInput.value = data.total_pages;
            endPageInput.max = data.total_pages;
            if (totalPagesHint) {
                totalPagesHint.textContent = `${data.total_pages} páginas detectadas`;
            }
            segundosPorPagina = data.segundos_por_pagina || null;
            actualizarEstimacion();

            checkReadyToAudit();
        } catch (err) {
            console.error(err);
            pdfFileName.textContent = 'Error al leer el PDF';
            alert('Error al contar páginas del PDF: ' + err.message);
        }
    }

    function populateSelectors(rowPreviews, detectedRowIdx, headers, keyColDefault, compareColsDefault) {
        // Row selector
        rowSelector.innerHTML = '';
        rowPreviews.forEach(opt => {
            const option = document.createElement('option');
            option.value = opt.index;
            option.textContent = opt.preview;
            if (opt.index === detectedRowIdx) option.selected = true;
            rowSelector.appendChild(option);
        });

        // Key selector
        keyColSelector.innerHTML = '';
        headers.forEach(h => {
            const option = document.createElement('option');
            option.value = h;
            option.textContent = h;
            if (h === keyColDefault) option.selected = true;
            keyColSelector.appendChild(option);
        });

        // Compare multi selector
        compareColsSelector.innerHTML = '';
        headers.forEach(h => {
            if (h === keyColSelector.value) return;
            const option = document.createElement('option');
            option.value = h;
            option.textContent = h;
            if (compareColsDefault.includes(h)) option.selected = true;
            compareColsSelector.appendChild(option);
        });
        
        // Listen to key column change to refresh compare list
        keyColSelector.addEventListener('change', () => {
            const val = keyColSelector.value;
            compareColsSelector.innerHTML = '';
            headers.forEach(h => {
                if (h === val) return;
                const option = document.createElement('option');
                option.value = h;
                option.textContent = h;
                compareColsSelector.appendChild(option);
            });
        });
    }

    function renderPreviewTable(previewData, headers) {
        const thead = excelPreviewTable.querySelector('thead');
        const tbody = excelPreviewTable.querySelector('tbody');
        
        thead.innerHTML = '';
        tbody.innerHTML = '';
        
        if (headers.length === 0 || previewData.length === 0) return;
        
        // Headers row
        const trHeader = document.createElement('tr');
        headers.slice(0, 6).forEach(h => {
            const th = document.createElement('th');
            th.textContent = h;
            trHeader.appendChild(th);
        });
        if (headers.length > 6) {
            const th = document.createElement('th');
            th.textContent = '...';
            trHeader.appendChild(th);
        }
        thead.appendChild(trHeader);
        
        // Data rows
        previewData.forEach(row => {
            const tr = document.createElement('tr');
            headers.slice(0, 6).forEach(h => {
                const td = document.createElement('td');
                td.textContent = row[h] !== undefined ? row[h] : '';
                tr.appendChild(td);
            });
            if (headers.length > 6) {
                const td = document.createElement('td');
                td.textContent = '...';
                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        });
    }

    function checkReadyToAudit() {
        if (excelFileObj && pdfFileObj) {
            actionPanel.classList.remove('hidden');
        } else {
            actionPanel.classList.add('hidden');
        }
    }

    // Start Audit
    startAuditBtn.addEventListener('click', async () => {
        if (!excelFileObj || !pdfFileObj) return;

        // Get selected compare columns
        const selectedCompare = Array.from(compareColsSelector.selectedOptions).map(opt => opt.value);
        if (selectedCompare.length === 0) {
            alert('Por favor selecciona al menos una columna para comparar.');
            return;
        }

        const formData = new FormData();
        formData.append('excel_file', excelFileObj);
        formData.append('pdf_file', pdfFileObj);
        formData.append('selected_row_idx', rowSelector.value);
        formData.append('key_col', keyColSelector.value);
        formData.append('compare_cols', selectedCompare.join(','));
        formData.append('similarity_threshold', similarityThresholdInput.value);
        formData.append('start_page', startPageInput.value);
        formData.append('end_page', endPageInput.value);
        formData.append('pdf_dpi', pdfDpiInput.value);
        formData.append('img_filter', imgFilterInput.value);

        // Clear and initialize console tab image
        const consoleImg = document.getElementById('console_ocr_image');
        if (consoleImg) {
            consoleImg.src = '';
            consoleImg.alt = 'Procesando documentos...';
        }
        const consoleStatus = document.getElementById('console_ocr_status');
        if (consoleStatus) {
            consoleStatus.textContent = 'Procesando...';
            consoleStatus.className = 'status-indicator processing';
        }
        
        const cardsList = document.getElementById('extracted_cards_list');
        if (cardsList) {
            cardsList.innerHTML = '<div class="empty-state">Iniciando validación y leyendo páginas...</div>';
        }
        
        selectedRawTextPage = null;
        userSelectedCardManually = false;
        editingDocument = null;
        excelRecordsCache = null;

        // Switch to the console tab
        const consoleTabBtn = document.getElementById('main_tab_btn_console');
        if (consoleTabBtn) {
            consoleTabBtn.click();
        }

        // Hide layout & Show progress
        actionPanel.classList.add('hidden');
        resultsSection.classList.add('hidden');
        progressSection.classList.remove('hidden');
        updateProgress(0, 'Subiendo archivos e iniciando procesamiento...', 0);
        startClientTimer();

        try {
            const response = await fetch('/api/start-audit', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                throw new Error(await extractErrorDetail(response, 'Error al iniciar el proceso'));
            }
            const data = await response.json();
            currentTaskId = data.task_id;
            
            // Start polling status
            pollInterval = setInterval(pollAuditStatus, 1000);
        } catch (err) {
            console.error(err);
            progressSection.classList.add('hidden');
            actionPanel.classList.remove('hidden');
            alert('Error en validación: ' + err.message);
        }
    });

    async function pollAuditStatus() {
        if (!currentTaskId) return;
        
        try {
            const response = await fetch(`/api/status/${currentTaskId}`);
            if (!response.ok) throw new Error('Error al consultar estado');
            
            const data = await response.json();
            
            if (data.status === 'processing' || data.status === 'queued') {
                const statusText = data.status_detail || `Procesando página ${data.current_page}...`;
                updateProgress(data.progress, statusText, data.current_page);

                // Tiempo restante que calcula el backend con el ritmo real de ESTA
                // corrida (se afina solo a medida que avanza).
                if (etaRestante) {
                    if (data.eta_seconds != null && data.eta_seconds > 0) {
                        etaRestante.textContent = `faltan ~${formatDuration(data.eta_seconds)}`;
                        etaRestante.classList.remove('hidden');
                    } else {
                        etaRestante.classList.add('hidden');
                    }
                }
                
                // Real-time OCR raw text and card display
                if (data.live_results && data.live_results.length > 0) {
                    renderExtractedCards(data.live_results);
                }
            } else if (data.status === 'completed') {
                clearInterval(pollInterval);
                stopClientTimer(data.elapsed_seconds);
                updateProgress(100, 'Auditoría completada exitosamente.', data.current_page);
                if (etaRestante) etaRestante.classList.add('hidden');
                
                // Final console status update
                const consoleStatus = document.getElementById('console_ocr_status');
                if (consoleStatus) {
                    consoleStatus.textContent = 'Completado';
                    consoleStatus.className = 'status-indicator completed';
                }
                
                // Render final console results one last time
                if (data.live_results && data.live_results.length > 0) {
                    renderExtractedCards(data.live_results);
                }
                
                setTimeout(() => {
                    progressSection.classList.add('hidden');
                    renderResults(data);
                    // Automatically switch to comparative results tab
                    const resultsTabBtn = document.getElementById('main_tab_btn_results');
                    if (resultsTabBtn) {
                        resultsTabBtn.click();
                    }
                }, 1500);
            } else if (data.status === 'error') {
                clearInterval(pollInterval);
                if (clientTimerInterval) { clearInterval(clientTimerInterval); clientTimerInterval = null; }
                progressSection.classList.add('hidden');
                actionPanel.classList.remove('hidden');
                alert('La auditoría falló: ' + data.error);
                
                const consoleStatus = document.getElementById('console_ocr_status');
                if (consoleStatus) {
                    consoleStatus.textContent = 'Error';
                    consoleStatus.className = 'status-indicator error';
                }
            }
        } catch (err) {
            console.error(err);
        }
    }

    function formatDuration(totalSeconds) {
        if (totalSeconds == null) return '';
        const s = Math.round(totalSeconds);
        const m = Math.floor(s / 60);
        const rem = s % 60;
        return m > 0 ? `${m}m ${rem}s` : `${rem}s`;
    }

    // Estimacion ANTES de arrancar. Se calcula con el ritmo real de corridas previas en
    // esta maquina (lo manda el backend); la primera vez no hay historial y se dice
    // claramente, en vez de inventar un numero que quedaria lejos.
    function actualizarEstimacion() {
        if (!estimacionTiempo || !estimacionTiempoTexto) return;
        if (!pdfFileObj) {
            estimacionTiempo.classList.add('hidden');
            return;
        }

        const desde = parseInt(startPageInput.value, 10) || 1;
        const hasta = parseInt(endPageInput.value, 10) || desde;
        const paginas = Math.max(0, hasta - desde + 1);
        if (paginas <= 0) {
            estimacionTiempo.classList.add('hidden');
            return;
        }

        if (segundosPorPagina) {
            const total = segundosPorPagina * paginas;
            // Se muestra como rango (+-20%) en vez de un numero exacto: el tiempo real
            // varia con la calidad del escaneo y con lo que este haciendo el equipo, y
            // un rango honesto envejece mejor que una cifra que casi nunca acierta.
            estimacionTiempoTexto.textContent =
                `Tiempo estimado: ${formatDuration(total * 0.8)} – ${formatDuration(total * 1.2)} para ${paginas} página(s)`;
        } else {
            estimacionTiempoTexto.textContent =
                `${paginas} página(s) por procesar. La primera vez no hay con qué estimar el tiempo; ` +
                `a partir de la próxima se calcula con el ritmo real de este equipo.`;
        }
        estimacionTiempo.classList.remove('hidden');
    }

    startPageInput.addEventListener('input', actualizarEstimacion);
    endPageInput.addEventListener('input', actualizarEstimacion);

    function startClientTimer() {
        clientTimerStartedAt = Date.now();
        if (elapsedTimerEl) elapsedTimerEl.innerHTML = '<svg class="icon"><use href="#icon-clock"/></svg> 0s';
        if (elapsedFinalEl) elapsedFinalEl.textContent = '';
        if (clientTimerInterval) clearInterval(clientTimerInterval);
        clientTimerInterval = setInterval(() => {
            const secs = (Date.now() - clientTimerStartedAt) / 1000;
            if (elapsedTimerEl) {
                elapsedTimerEl.innerHTML = `<svg class="icon"><use href="#icon-clock"/></svg> ${formatDuration(secs)}`;
            }
        }, 1000);
    }

    function stopClientTimer(serverElapsedSeconds) {
        if (clientTimerInterval) {
            clearInterval(clientTimerInterval);
            clientTimerInterval = null;
        }
        const finalSeconds = serverElapsedSeconds != null
            ? serverElapsedSeconds
            : (clientTimerStartedAt ? (Date.now() - clientTimerStartedAt) / 1000 : null);
        if (elapsedFinalEl) {
            elapsedFinalEl.innerHTML = finalSeconds != null
                ? `<svg class="icon"><use href="#icon-clock"/></svg> Tiempo total: ${formatDuration(finalSeconds)}`
                : '';
        }
    }

    function updateProgress(percent, text, currentPage) {
        progressBar.style.width = `${percent}%`;
        progressPercent.textContent = `${percent}%`;
        progressStatus.textContent = text;
        progressSubtext.textContent = `Progreso global del lote | Página procesada: ${currentPage}`;
    }

    // Campos de una tarjeta que se pueden editar directamente (corrige un dato mal
    // leido por el OCR). "side" y "edad" quedan fuera: "side" se deriva de las caras
    // detectadas y "edad" se recalcula sola en el server cuando cambia "date".
    const CARD_FIELDS = [
        { key: 'document', label: 'Documento', editable: true },
        { key: 'name', label: 'Nombre', editable: true },
        { key: 'date', label: 'Fecha Nac.', editable: true },
        { key: 'side', label: 'Cara', editable: false },
        { key: 'tipo_documento', label: 'Tipo Doc.', editable: true },
        { key: 'edad', label: 'Edad', editable: false },
        { key: 'lugar_nacimiento', label: 'Lugar Nac.', editable: true },
        { key: 'sexo', label: 'Sexo', editable: true },
        { key: 'estatura', label: 'Estatura', editable: true },
        { key: 'grupo_sanguineo', label: 'RH', editable: true },
        { key: 'fecha_lugar_expedicion', label: 'Expedición', editable: true },
    ];

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function buildCardFieldsHtml(res, editMode, fields = CARD_FIELDS) {
        return fields.map(f => {
            const val = res[f.key] || '';
            if (editMode && f.editable) {
                return `
                    <div class="card-field">
                        <span class="card-field-label">${f.label}</span>
                        <input type="text" class="card-field-input" data-field="${f.key}" value="${escapeHtml(val)}">
                    </div>`;
            }
            return `
                <div class="card-field">
                    <span class="card-field-label">${f.label}</span>
                    <span class="card-field-value">${escapeHtml(val) || 'No detectado'}</span>
                </div>`;
        }).join('');
    }

    function buildSuggestionHtml(res) {
        if (!res.sugerencia_documento && !res.sugerencia_nombre) return '';
        const partes = [];
        if (res.sugerencia_documento) partes.push(`documento <strong>${escapeHtml(res.sugerencia_documento)}</strong>`);
        if (res.sugerencia_nombre) partes.push(`nombre <strong>${escapeHtml(res.sugerencia_nombre)}</strong>`);
        const confianza = res.sugerencia_confianza != null ? ` (${Math.round(res.sugerencia_confianza)}% de coincidencia con el Excel)` : '';
        return `
            <div class="card-suggestion">
                <svg class="icon"><use href="#icon-lightbulb"/></svg>
                <span>¿Será ${partes.join(' y ')}${confianza}?</span>
                <button type="button" class="btn btn-secondary card-suggestion-apply"
                        data-document="${escapeHtml(res.sugerencia_documento || '')}"
                        data-nombre="${escapeHtml(res.sugerencia_nombre || '')}">
                    Aplicar
                </button>
            </div>`;
    }

    async function guardarEdicionTarjeta(documentoActual, edits) {
        if (!currentTaskId || Object.keys(edits).length === 0) {
            editingDocument = null;
            renderExtractedCards(lastLiveResults);
            return;
        }
        try {
            const response = await fetch(`/api/task/${currentTaskId}/records/${encodeURIComponent(documentoActual)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(edits)
            });
            if (!response.ok) {
                alert(await extractErrorDetail(response, 'No se pudo guardar la corrección.'));
                return;
            }
            const data = await response.json();
            editingDocument = null;
            lastLiveResults = data.live_results || [];
            renderResults(data);
            renderExtractedCards(lastLiveResults);
        } catch (err) {
            console.error(err);
            alert('Error al guardar la corrección.');
        }
    }

    // Filtro combinado (buscador + "solo con sugerencias") aplicado antes de dibujar
    // la lista -- lastLiveResults siempre guarda el set COMPLETO sin filtrar, para que
    // cambiar el filtro no dependa de volver a pedirle datos al servidor.
    function getFilteredLiveResults(liveResults) {
        return liveResults.filter(res => {
            if (onlySuggestions && !(res.sugerencia_documento || res.sugerencia_nombre)) return false;
            if (cardSearchQuery) {
                const q = cardSearchQuery.toLowerCase();
                const doc = (res.document || '').toLowerCase();
                const name = (res.name || '').toLowerCase();
                if (!doc.includes(q) && !name.includes(q)) return false;
            }
            return true;
        });
    }

    function renderExtractedCards(liveResults) {
        const cardsList = document.getElementById('extracted_cards_list');
        if (!cardsList) return;

        lastLiveResults = liveResults;

        // Save scroll position to prevent scrolling jumps
        const scrollTop = cardsList.scrollTop;

        // Auto-show image of the latest processed card if user hasn't clicked any card manually
        if (liveResults.length > 0 && !userSelectedCardManually) {
            const latestRes = liveResults[liveResults.length - 1];
            if (latestRes.images && latestRes.images.length > 0) {
                const latestImg = latestRes.images[latestRes.images.length - 1];
                selectedRawTextPage = latestImg.page;
                const consoleImg = document.getElementById('console_ocr_image');
                if (consoleImg && latestImg.url) {
                    consoleImg.src = latestImg.url;
                    consoleImg.alt = `Página ${latestImg.page}`;
                }
            }
        }

        const filteredResults = getFilteredLiveResults(liveResults);

        cardsList.innerHTML = '';

        if (filteredResults.length === 0) {
            cardsList.innerHTML = liveResults.length === 0
                ? '<div class="empty-state">No se ha iniciado la validación. Los datos extraídos aparecerán aquí.</div>'
                : '<div class="empty-state">Ningún resultado coincide con el filtro.</div>';
            return;
        }

        filteredResults.forEach(res => {
            const card = document.createElement('div');
            const isEditing = editingDocument === res.document;
            card.className = 'extracted-card' + (isEditing ? ' editing' : '');
            if (res.pages.includes(selectedRawTextPage)) {
                card.classList.add('selected');
            }
            card.setAttribute('data-pages', res.pages.join(','));
            card.setAttribute('data-document', res.document);

            let imagesSelectorHtml = '';
            if (res.images && res.images.length > 1) {
                imagesSelectorHtml = `
                    <div class="card-image-selector" style="margin-top: 12px; display: flex; gap: 8px; border-top: 1px dashed var(--card-border); padding-top: 10px;">
                        ${res.images.map((img, idx) => `
                            <button class="btn btn-secondary btn-small img-toggle-btn ${img.page === selectedRawTextPage ? 'active' : ''}"
                                    data-url="${img.url}"
                                    data-page="${img.page}"
                                    style="padding: 4px 8px; font-size: 0.72rem; flex: 1; min-height: 26px; border: 1px solid var(--card-border); border-radius: 4px; background: rgba(255,255,255,0.02); color: var(--text-secondary); cursor: pointer;">
                                Cara ${idx + 1} (Pág. ${img.page})
                            </button>
                        `).join('')}
                    </div>
                `;
            }

            const actionsHtml = isEditing
                ? `<div class="card-edit-actions">
                       <button type="button" class="btn-icon-only card-save-btn" title="Guardar"><svg class="icon"><use href="#icon-check"/></svg></button>
                       <button type="button" class="btn-icon-only card-cancel-btn" title="Cancelar"><svg class="icon"><use href="#icon-x"/></svg></button>
                   </div>`
                : `<button type="button" class="btn-icon-only card-edit-btn" title="Editar"><svg class="icon"><use href="#icon-edit"/></svg></button>`;

            const tieneSugerencia = !isEditing && (res.sugerencia_documento || res.sugerencia_nombre);
            const suggestionBadgeHtml = tieneSugerencia
                ? `<span class="card-suggestion-badge" title="Tiene una sugerencia de corrección pendiente"><svg class="icon"><use href="#icon-lightbulb"/></svg></span>`
                : '';

            card.innerHTML = `
                <div class="card-title-row">
                    <span class="card-page-badge"><svg class="icon"><use href="#icon-file-text"/></svg> Págs. ${res.pages.join(', ')}</span>
                    <div class="card-title-actions">
                        ${suggestionBadgeHtml}
                        <span class="card-method-badge ${res.method === 'Fallo' ? 'fallo' : ''}">${res.method}</span>
                        <button type="button" class="btn-icon-only card-raw-text-btn" title="Ver texto OCR crudo"><svg class="icon"><use href="#icon-eye"/></svg></button>
                        ${actionsHtml}
                    </div>
                </div>
                <div class="card-fields-grid">
                    ${buildCardFieldsHtml(res, isEditing, CARD_FIELDS)}
                </div>
                ${!isEditing ? buildSuggestionHtml(res) : ''}
                ${imagesSelectorHtml}
            `;

            // Add click listener to inspect image of this card
            card.addEventListener('click', (e) => {
                // Botones/inputs interactivos: manejar aparte y no disparar la seleccion de tarjeta
                if (e.target.closest('.card-field-input')) {
                    e.stopPropagation();
                    return;
                }

                const rawTextBtn = e.target.closest('.card-raw-text-btn');
                if (rawTextBtn) {
                    e.stopPropagation();
                    openRawTextModal(res);
                    return;
                }

                const editBtn = e.target.closest('.card-edit-btn');
                if (editBtn) {
                    e.stopPropagation();
                    editingDocument = res.document;
                    renderExtractedCards(lastLiveResults);
                    return;
                }

                const cancelBtn = e.target.closest('.card-cancel-btn');
                if (cancelBtn) {
                    e.stopPropagation();
                    editingDocument = null;
                    renderExtractedCards(lastLiveResults);
                    return;
                }

                const saveBtn = e.target.closest('.card-save-btn');
                if (saveBtn) {
                    e.stopPropagation();
                    const edits = {};
                    card.querySelectorAll('.card-field-input').forEach(input => {
                        const field = input.getAttribute('data-field');
                        const valorNuevo = input.value.trim();
                        if (valorNuevo !== (res[field] || '')) {
                            edits[field] = valorNuevo;
                        }
                    });
                    guardarEdicionTarjeta(res.document, edits);
                    return;
                }

                const suggestionBtn = e.target.closest('.card-suggestion-apply');
                if (suggestionBtn) {
                    e.stopPropagation();
                    const edits = {};
                    const doc = suggestionBtn.getAttribute('data-document');
                    const nombre = suggestionBtn.getAttribute('data-nombre');
                    if (doc) edits.document = doc;
                    if (nombre) edits.name = nombre;
                    guardarEdicionTarjeta(res.document, edits);
                    return;
                }

                // If clicked on an image toggle button, don't trigger full card reload
                const toggleBtn = e.target.closest('.img-toggle-btn');
                if (toggleBtn) {
                    e.stopPropagation();
                    const url = toggleBtn.getAttribute('data-url');
                    const page = parseInt(toggleBtn.getAttribute('data-page'));

                    // Highlight selected button inside this card
                    card.querySelectorAll('.img-toggle-btn').forEach(btn => btn.classList.remove('active'));
                    toggleBtn.classList.add('active');

                    selectedRawTextPage = page;

                    // Keep card selected
                    document.querySelectorAll('.extracted-card').forEach(c => c.classList.remove('selected'));
                    card.classList.add('selected');

                    const consoleImg = document.getElementById('console_ocr_image');
                    if (consoleImg && url) {
                        consoleImg.src = url;
                        consoleImg.alt = `Página ${page}`;
                    }
                    return;
                }

                if (isEditing) return; // no cambiar la imagen mientras se esta editando

                document.querySelectorAll('.extracted-card').forEach(c => c.classList.remove('selected'));
                card.classList.add('selected');
                userSelectedCardManually = true;

                if (res.images && res.images.length > 0) {
                    selectedRawTextPage = res.images[0].page;
                    const consoleImg = document.getElementById('console_ocr_image');
                    if (consoleImg && res.images[0].url) {
                        consoleImg.src = res.images[0].url;
                        consoleImg.alt = `Página ${res.images[0].page}`;
                    }

                    // Highlight the first button
                    card.querySelectorAll('.img-toggle-btn').forEach((btn, idx) => {
                        if (idx === 0) btn.classList.add('active');
                        else btn.classList.remove('active');
                    });
                }
            });

            cardsList.appendChild(card);
        });

        // Restore scroll position
        cardsList.scrollTop = scrollTop;
    }

    // Panel "Personas del Excel" -- lista de la base cargada, con detalle expandible
    async function loadExcelRecords() {
        const container = document.getElementById('excel_records_list');
        if (!container || !currentTaskId) return;

        if (excelRecordsCache) {
            renderExcelRecords(excelRecordsCache);
            return;
        }

        container.innerHTML = '<div class="empty-state">Cargando personas del Excel...</div>';
        try {
            const response = await fetch(`/api/task/${currentTaskId}/excel-records`);
            if (!response.ok) {
                container.innerHTML = `<div class="empty-state">${escapeHtml(await extractErrorDetail(response, 'No se pudo cargar el Excel.'))}</div>`;
                return;
            }
            const data = await response.json();
            excelRecordsCache = data.records || [];
            renderExcelRecords(excelRecordsCache);
        } catch (err) {
            console.error(err);
            container.innerHTML = '<div class="empty-state">Error al cargar las personas del Excel.</div>';
        }
    }

    function renderExcelRecords(records) {
        const container = document.getElementById('excel_records_list');
        if (!container) return;

        if (!records || records.length === 0) {
            container.innerHTML = '<div class="empty-state">No hay personas para mostrar.</div>';
            return;
        }

        const filteredRecords = cardSearchQuery
            ? records.filter(rec => {
                const q = cardSearchQuery.toLowerCase();
                const doc = (rec.documento || '').toLowerCase();
                const name = (rec.nombre || '').toLowerCase();
                return doc.includes(q) || name.includes(q);
            })
            : records;

        if (filteredRecords.length === 0) {
            container.innerHTML = '<div class="empty-state">Ningún resultado coincide con el filtro.</div>';
            return;
        }

        container.innerHTML = '';
        filteredRecords.forEach(rec => {
            const row = document.createElement('div');
            row.className = 'extracted-card excel-record-row';

            const detalleCampos = Object.entries(rec.columnas || {})
                .map(([col, val]) => `
                    <div class="card-field">
                        <span class="card-field-label">${escapeHtml(col)}</span>
                        <span class="card-field-value">${escapeHtml(val) || '—'}</span>
                    </div>`)
                .join('');

            row.innerHTML = `
                <div class="card-title-row">
                    <span class="card-page-badge"><svg class="icon"><use href="#icon-user"/></svg> ${escapeHtml(rec.documento) || 'Sin documento'}</span>
                    <span class="excel-record-toggle">Ver más</span>
                </div>
                <div class="card-fields-grid">
                    <div class="card-field">
                        <span class="card-field-label">Nombre</span>
                        <span class="card-field-value">${escapeHtml(rec.nombre) || 'No disponible'}</span>
                    </div>
                </div>
                <div class="excel-record-details card-fields-grid">
                    ${detalleCampos}
                </div>
            `;

            row.addEventListener('click', () => {
                row.classList.toggle('expanded');
                const toggle = row.querySelector('.excel-record-toggle');
                if (toggle) toggle.textContent = row.classList.contains('expanded') ? 'Ver menos' : 'Ver más';
            });

            container.appendChild(row);
        });
    }

    function setConsoleSource(source) {
        currentSource = source;
        const pdfBtn = document.getElementById('source_toggle_pdf');
        const excelBtn = document.getElementById('source_toggle_excel');
        const pdfList = document.getElementById('extracted_cards_list');
        const excelList = document.getElementById('excel_records_list');
        const title = document.getElementById('console_right_title');
        const onlySuggestionsBtn = document.getElementById('only_suggestions_btn');

        if (pdfBtn) pdfBtn.classList.toggle('active', source === 'pdf');
        if (excelBtn) excelBtn.classList.toggle('active', source === 'excel');
        if (pdfList) pdfList.classList.toggle('hidden', source !== 'pdf');
        if (excelList) excelList.classList.toggle('hidden', source !== 'excel');
        if (title) title.textContent = source === 'pdf' ? 'Datos Extraídos de Cédulas' : 'Personas del Excel';
        // "Con sugerencias" solo tiene sentido para lo leido del PDF -- el Excel no tiene sugerencias
        if (onlySuggestionsBtn) onlySuggestionsBtn.classList.toggle('hidden', source !== 'pdf');

        if (source === 'excel') loadExcelRecords();
    }

    const sourceTogglePdf = document.getElementById('source_toggle_pdf');
    const sourceToggleExcel = document.getElementById('source_toggle_excel');
    if (sourceTogglePdf) sourceTogglePdf.addEventListener('click', () => setConsoleSource('pdf'));
    if (sourceToggleExcel) sourceToggleExcel.addEventListener('click', () => setConsoleSource('excel'));

    // Buscador por cédula/nombre (aplica a la lista que este visible, PDF o Excel) y
    // filtro "solo con sugerencias" (solo aplica al lado del PDF).
    const cardSearchInput = document.getElementById('card_search_input');
    const onlySuggestionsBtn = document.getElementById('only_suggestions_btn');

    function rerenderCurrentSourceList() {
        if (currentSource === 'pdf') {
            renderExtractedCards(lastLiveResults);
        } else if (excelRecordsCache) {
            renderExcelRecords(excelRecordsCache);
        }
    }

    if (cardSearchInput) {
        cardSearchInput.addEventListener('input', (e) => {
            cardSearchQuery = e.target.value.trim();
            rerenderCurrentSourceList();
        });
    }

    if (onlySuggestionsBtn) {
        onlySuggestionsBtn.addEventListener('click', () => {
            onlySuggestions = !onlySuggestions;
            onlySuggestionsBtn.classList.toggle('active', onlySuggestions);
            renderExtractedCards(lastLiveResults);
        });
    }

    function renderResults(data) {
        // Set metrics
        metricCorrect.textContent = data.metrics.correct;
        metricAlerts.textContent = data.metrics.alerts;
        metricMissing.textContent = data.metrics.missing;
        metricHuerfanos.textContent = data.metrics.huerfanos;

        // Render Tables
        renderResultTable('table_coinciden', data.results.coinciden, ['Página_PDF', 'Identificación_Excel', 'Nombre_Excel', 'Identificación_PDF', 'Nombre_PDF', 'Similitud_Nombre_%']);
        renderResultTable('table_anomalias', data.results.anomalias, ['Página_PDF', 'Identificación_Excel', 'Nombre_Excel', 'Identificación_PDF', 'Nombre_PDF', 'Similitud_Nombre_%', 'Alerta_Detalle']);
        renderResultTable('table_solo_excel', data.results.solo_excel, ['Identificación_Excel', 'Nombre_Excel']);
        renderResultTable('table_solo_pdf', data.results.solo_pdf, ['Página_PDF', 'Identificación_PDF', 'Nombre_PDF']);

        resultsSection.classList.remove('hidden');
        actionPanel.classList.remove('hidden');
    }

    // Las auditorias viejas quedaron guardadas en la base con el puntaje crudo de
    // rapidfuzz (69.76744186046511). El servidor ya lo redondea al calcularlo, pero eso
    // solo aplica a corridas nuevas -- por eso tambien se limpia aca al pintar, para que
    // el historial se vea igual de prolijo.
    function formatearPorcentaje(valor) {
        const num = parseFloat(valor);
        if (isNaN(num)) return valor;
        return Number.isInteger(num) ? String(num) : num.toFixed(1);
    }

    // Mismo caso, pero para los porcentajes incrustados en el texto de la alerta.
    function limpiarPorcentajesEnTexto(texto) {
        return String(texto).replace(/(\d+\.\d+)\s*%/g, (_, n) => `${formatearPorcentaje(n)}%`);
    }

    function renderResultTable(tableId, dataList, keys) {
        const table = document.getElementById(tableId);
        const tbody = table.querySelector('tbody');
        tbody.innerHTML = '';

        if (!dataList || dataList.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = keys.length;
            td.textContent = 'Ningún registro en esta categoría';
            td.style.textAlign = 'center';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }

        dataList.forEach(item => {
            const tr = document.createElement('tr');
            keys.forEach(k => {
                const td = document.createElement('td');
                if (k === 'Similitud_Nombre_%') {
                    const score = item[k];
                    td.innerHTML = `<span class="badge-cell ${score >= similarityThresholdInput.value ? 'success' : 'danger'}">${formatearPorcentaje(score)}%</span>`;
                } else if (k === 'Alerta_Detalle') {
                    td.textContent = item[k] ? limpiarPorcentajesEnTexto(item[k]) : '';
                } else if (k === 'Página_PDF') {
                    const pageNum = item[k];
                    if (pageNum && pageNum !== 'N/A') {
                        td.innerHTML = `<a href="#" class="pdf-page-link" data-page="${pageNum}"><svg class="icon"><use href="#icon-file-text"/></svg> Pág. ${pageNum}</a>`;
                    } else {
                        td.textContent = 'N/A';
                    }
                } else {
                    td.textContent = item[k] !== undefined && item[k] !== null ? item[k] : '';
                }
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
    }

    // Delegated click event for pdf-page-link: switches to the console tab and highlights that card
    document.addEventListener('click', (e) => {
        const link = e.target.closest('.pdf-page-link');
        if (link) {
            e.preventDefault();
            const pageNum = parseInt(link.getAttribute('data-page'));
            if (pageNum) {
                // Switch to the console tab
                const consoleTabBtn = document.getElementById('main_tab_btn_console');
                if (consoleTabBtn) {
                    consoleTabBtn.click();
                }
                
                // Find the card that contains this page number
                let targetCard = null;
                document.querySelectorAll('.extracted-card').forEach(card => {
                    const pageAttr = card.getAttribute('data-pages');
                    if (pageAttr) {
                        const pagesArray = pageAttr.split(',').map(p => p.trim());
                        if (pagesArray.includes(pageNum.toString())) {
                            targetCard = card;
                        }
                    }
                });
                
                if (targetCard) {
                    targetCard.click();
                    targetCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                    
                    // Also programmatically click the specific page toggle button inside the card if it exists!
                    const toggleBtn = targetCard.querySelector(`.img-toggle-btn[data-page="${pageNum}"]`);
                    if (toggleBtn) {
                        toggleBtn.click();
                    }
                }
            }
        }
    });

    // Download Report Action
    downloadReportBtn.addEventListener('click', () => {
        if (!currentTaskId) return;
        window.open(`/api/download/${currentTaskId}`, '_blank');
    });

    // Main tabs functionality
    const mainTabBtns = document.querySelectorAll('.main-tab-btn');
    const mainTabContents = document.querySelectorAll('.main-tab-content');
    
    mainTabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.getAttribute('data-main-tab');

            mainTabBtns.forEach(b => b.classList.remove('active'));
            mainTabContents.forEach(c => c.classList.remove('active'));

            btn.classList.add('active');
            document.getElementById(targetTab).classList.add('active');

            if (targetTab === 'tab_history') {
                loadHistoryList();
            }
        });
    });

    // Historial: cargar la lista de auditorías guardadas
    async function loadHistoryList() {
        const container = document.getElementById('history_list');
        container.innerHTML = '<div class="empty-state">Cargando historial...</div>';
        try {
            const response = await fetch('/api/history');
            if (!response.ok) throw new Error('Error al consultar el historial');
            const data = await response.json();

            if (!data.audits || data.audits.length === 0) {
                container.innerHTML = '<div class="empty-state">Aún no hay auditorías guardadas.</div>';
                return;
            }

            container.innerHTML = '';
            data.audits.forEach(a => {
                const item = document.createElement('div');
                item.className = 'extracted-card';

                const fecha = new Date(a.created_at).toLocaleString('es-CO');

                item.innerHTML = `
                    <div class="card-title-row">
                        <span class="card-page-badge"><svg class="icon"><use href="#icon-calendar"/></svg> ${fecha}</span>
                        <button type="button" class="btn btn-secondary history-delete-btn" data-task-id="${a.task_id}" style="padding:4px 10px; min-height: 26px;"><svg class="icon"><use href="#icon-trash"/></svg> Eliminar</button>
                    </div>
                    <div class="card-fields-grid">
                        <div class="card-field">
                            <span class="card-field-label">Excel</span>
                            <span class="card-field-value">${a.excel_filename || '—'}</span>
                        </div>
                        <div class="card-field">
                            <span class="card-field-label">PDF</span>
                            <span class="card-field-value">${a.pdf_filename || '—'}</span>
                        </div>
                        <div class="card-field">
                            <span class="card-field-label">Verificados</span>
                            <span class="card-field-value">${a.correct_count}</span>
                        </div>
                        <div class="card-field">
                            <span class="card-field-label">Alertas</span>
                            <span class="card-field-value">${a.alerts_count}</span>
                        </div>
                        <div class="card-field">
                            <span class="card-field-label">Faltantes</span>
                            <span class="card-field-value">${a.missing_count}</span>
                        </div>
                        <div class="card-field">
                            <span class="card-field-label">Huérfanos</span>
                            <span class="card-field-value">${a.huerfanos_count}</span>
                        </div>
                    </div>
                `;

                item.addEventListener('click', (e) => {
                    if (e.target.closest('.history-delete-btn')) return;
                    openHistoryEntry(a.task_id);
                });

                container.appendChild(item);
            });

            container.querySelectorAll('.history-delete-btn').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    if (!confirm('¿Eliminar esta auditoría del historial? También se borra el escaneo cacheado de ese PDF, así que volver a auditarlo lo procesará desde cero. Esta acción no se puede deshacer.')) return;
                    try {
                        await fetch(`/api/history/${btn.dataset.taskId}`, { method: 'DELETE' });
                    } catch (err) {
                        console.error(err);
                    }
                    loadHistoryList();
                });
            });
        } catch (err) {
            console.error(err);
            container.innerHTML = '<div class="empty-state">Error al cargar el historial.</div>';
        }
    }

    // Historial: reabrir una auditoría guardada sin volver a correr OCR
    async function openHistoryEntry(taskId) {
        try {
            const response = await fetch(`/api/history/${taskId}`);
            if (!response.ok) throw new Error('No se pudo cargar esta auditoría');
            const data = await response.json();

            currentTaskId = taskId; // para que el botón de descarga apunte al reporte correcto
            excelRecordsCache = null; // el panel de Excel es de otra auditoría, forzar recarga

            renderResults(data);
            stopClientTimer(data.elapsed_seconds);
            if (data.live_results && data.live_results.length > 0) {
                renderExtractedCards(data.live_results);
            }

            // Esta auditoria del historial ya esta completa -- sin esto el badge se
            // quedaba pegado en "En espera..." (su valor por defecto) porque solo se
            // actualizaba durante una corrida en vivo, nunca al reabrir una ya guardada.
            const consoleStatus = document.getElementById('console_ocr_status');
            if (consoleStatus) {
                consoleStatus.textContent = 'Completado';
                consoleStatus.className = 'status-indicator completed';
            }

            const resultsTabBtn = document.getElementById('main_tab_btn_results');
            if (resultsTabBtn) resultsTabBtn.click();
        } catch (err) {
            console.error(err);
            alert('No se pudo cargar esta auditoría del historial.');
        }
    }

    // Results Tab buttons functionality
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');
    
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.getAttribute('data-tab');
            
            tabBtns.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));
            
            btn.classList.add('active');
            document.getElementById(targetTab).classList.add('active');
        });
    });

    // --- Cerrar la aplicación -------------------------------------------------------
    // El .exe se compila sin consola, así que no hay ninguna forma visible de apagarlo:
    // si la app quedó abierta en el navegador (el plan B cuando no se pudo abrir la
    // ventana propia), cerrar la pestaña deja el servidor corriendo invisible. Este
    // botón es la única salida que no exige abrir el Administrador de tareas.
    //
    // Solo se muestra cuando corre empaquetada -- en desarrollo el server se apaga con
    // Ctrl+C y un botón que mata el proceso sería un accidente esperando ocurrir.
    const closeAppBtn = document.getElementById('close_app_btn');
    if (closeAppBtn) {
        fetch('/api/app-info')
            .then(r => r.json())
            .then(info => {
                if (!info.empaquetada) return;
                closeAppBtn.classList.remove('hidden');
                iniciarLatido();
            })
            .catch(() => {});

        closeAppBtn.addEventListener('click', async () => {
            if (!confirm('¿Cerrar la aplicación? Se detendrá cualquier auditoría en curso.')) return;
            try {
                await fetch('/api/shutdown', { method: 'POST' });
            } catch (err) {
                // Se ignora a propósito: el servidor se está muriendo, así que lo normal
                // es que esta petición no alcance a responder.
            }
            document.getElementById('goodbye_overlay').classList.remove('hidden');
        });
    }

    // Le avisa al servidor que la interfaz sigue abierta. Si dejan de llegar estos
    // latidos, el servidor entiende que el usuario cerró la app y se apaga solo.
    //
    // Esto es lo que evita el proceso fantasma cuando la app quedó abierta en el
    // navegador: ahí no hay ninguna ventana propia que el .exe pueda vigilar, y sin
    // consola tampoco había forma de matarlo salvo el Administrador de tareas.
    function iniciarLatido() {
        const latir = () => { fetch('/api/heartbeat', { method: 'POST' }).catch(() => {}); };
        latir();
        // Cada 5 s. El servidor tolera hasta 90 s sin recibir nada, porque el navegador
        // ralentiza los temporizadores de una pestaña en segundo plano (hasta 1 por
        // minuto) y no queremos que minimizar la ventana apague la aplicación.
        setInterval(latir, 5000);

        // 'pagehide' es el único evento que el navegador garantiza al cerrar la pestaña,
        // y sendBeacon lo único que alcanza a salir mientras la página se está muriendo
        // (un fetch normal se cancela). Se manda a un endpoint aparte y no a /shutdown
        // porque este evento TAMBIÉN se dispara al recargar con F5: el servidor espera
        // unos segundos y, si llega un latido nuevo, cancela el apagado solo.
        window.addEventListener('pagehide', () => {
            try { navigator.sendBeacon('/api/despedida'); } catch (e) {}
        });
    }
});
