/*
  Scheduling UI glue for kiln-controller

  Features:
  - Pick date (calendar) and time
  - Quick buttons: today / tomorrow / day after tomorrow
  - Overwrite protection (backend deletes any existing scheduled job)
  - Status display + cancel button

  API:
    POST /api {cmd:'schedule', profile:'<name>', date:'YYYY-MM-DD', time:'HH:MM'}
    POST /api {cmd:'schedule_status'}
    POST /api {cmd:'schedule_cancel'}

  Requires: jQuery + jquery.bootstrap-growl (already loaded by index.html)
*/

function kcPad2(n) {
  return (n < 10) ? ('0' + n) : String(n);
}

function kcFormatDateYYYYMMDD(d) {
  return d.getFullYear() + '-' + kcPad2(d.getMonth() + 1) + '-' + kcPad2(d.getDate());
}

function kcGetSelectedProfileName() {
  var prof = (typeof selected_profile_name !== 'undefined') ? selected_profile_name : null;
  if (prof) return prof;

  // Fallback if selected_profile_name is not set for some reason
  try {
    if (typeof profiles !== 'undefined' && typeof selected_profile !== 'undefined' && profiles[selected_profile]) {
      return profiles[selected_profile].name;
    }
  } catch (e) {}

  return null;
}

function kcGrowl(msg, type, width, delay) {
  $.bootstrapGrowl(msg, {
    ele: 'body',
    type: type || 'info',
    offset: {from: 'top', amount: 250},
    align: 'center',
    width: width || 520,
    delay: delay || 6000,
    allow_dismiss: true,
    stackup_spacing: 10
  });
}


function kcGetScheduleDateTimeFromInputs() {
  var dateStr = ($('#schedule_date').val() || '').trim();
  var timeStr = ($('#schedule_time').val() || '').trim();

  if (!dateStr) return { error: 'Bitte ein Datum wählen.' };
  if (!timeStr) return { error: 'Bitte eine Uhrzeit wählen (HH:MM).' };

  var m = timeStr.match(/^([01]?\d|2[0-3]):([0-5]\d)$/);
  if (!m) return { error: 'Uhrzeit muss HH:MM (24h) sein.' };

  var parts = dateStr.split('-');
  if (parts.length !== 3) return { error: 'Datum muss YYYY-MM-DD sein.' };

  var y = parseInt(parts[0], 10);
  var mo = parseInt(parts[1], 10) - 1;
  var da = parseInt(parts[2], 10);
  var hh = parseInt(m[1], 10);
  var mm = parseInt(m[2], 10);

  var dt = new Date(y, mo, da, hh, mm, 0, 0);
  if (isNaN(dt.getTime())) return { error: 'Ungültiges Datum/Uhrzeit.' };

  // Strict past protection (do not auto-adjust)
  if (dt.getTime() <= (new Date()).getTime()) {
    return { error: 'Der gewählte Zeitpunkt liegt in der Vergangenheit.' };
  }

  return { dateStr: dateStr, timeStr: timeStr, dt: dt };
}

function kcRenderScheduleStatus(statusResp) {
  try {
    if (!statusResp || statusResp.success !== true) {
      $('#schedule_status_text').text('Schedule: Status unbekannt');
      $('#btn_schedule_cancel').hide();
      return;
    }

    if (!statusResp.scheduled) {
      $('#schedule_status_text').text('Schedule: nicht gesetzt');
      $('#btn_schedule_cancel').hide();
      return;
    }

    var when = statusResp.scheduled_for || '(unbekannt)';
    var prof = statusResp.profile || '(unbekanntes Profil)';
    var job = statusResp.job_id ? (' (Job ' + statusResp.job_id + ')') : '';

    $('#schedule_status_text').text('Schedule: ' + when + ' — ' + prof + job);
    $('#btn_schedule_cancel').show();
  } catch (e) {}
}

function kcRefreshScheduleStatus() {
  $.ajax({
    url: '/api',
    type: 'POST',
    contentType: 'application/json',
    dataType: 'json',
    data: JSON.stringify({ cmd: 'schedule_status' }),
    success: function(resp) {
      kcRenderScheduleStatus(resp);
    },
    error: function() {
      kcRenderScheduleStatus({ success: false });
    }
  });
}

function kcCancelSchedule() {
  try {
    $('#btn_schedule_cancel').prop('disabled', true);

    $.ajax({
      url: '/api',
      type: 'POST',
      contentType: 'application/json',
      dataType: 'json',
      data: JSON.stringify({ cmd: 'schedule_cancel' }),
      success: function(resp) {
        $('#btn_schedule_cancel').prop('disabled', false);

        if (!resp || resp.success !== true) {
          var err = (resp && resp.error) ? resp.error : 'Abbrechen fehlgeschlagen.';
          kcGrowl('ERROR: ' + err, 'error', 560, 7000);
          kcRefreshScheduleStatus();
          return;
        }

        var cnt = (typeof resp.removed_count !== 'undefined') ? resp.removed_count : null;
        var msg = (cnt === null) ? 'Schedule abgebrochen.' : ('Schedule abgebrochen. Entfernte Jobs: ' + cnt);
        kcGrowl(msg, 'success', 520, 6000);
        kcRefreshScheduleStatus();
      },
      error: function(xhr) {
        $('#btn_schedule_cancel').prop('disabled', false);
        var txt = (xhr && xhr.responseText) ? xhr.responseText : 'Abbrechen fehlgeschlagen.';
        kcGrowl('ERROR: ' + txt, 'error', 650, 8000);
        kcRefreshScheduleStatus();
      }
    });
  } catch (e) {
    $('#btn_schedule_cancel').prop('disabled', false);
    kcGrowl('ERROR: ' + String(e), 'error', 650, 8000);
  }
}

function kcScheduleRun() {
  try {
    // Guardrails
    if (typeof state !== 'undefined' && (state === 'RUNNING' || state === 'PAUSED')) {
      kcGrowl('Kann nicht schedulen solange ein Run aktiv ist. Bitte zuerst stoppen.', 'error', 520, 6500);
      return;
    }

    var prof = kcGetSelectedProfileName();
    if (!prof) {
      kcGrowl('Kein Profil ausgewählt.', 'error', 420, 6000);
      return;
    }

    var dtInfo = kcGetScheduleDateTimeFromInputs();
    if (dtInfo.error) {
      kcGrowl(dtInfo.error, 'error', 520, 6500);
      return;
    }

    var payload = { cmd: 'schedule', profile: prof, date: dtInfo.dateStr, time: dtInfo.timeStr };

    $('#btn_schedule').prop('disabled', true);

    $.ajax({
      url: '/api',
      type: 'POST',
      contentType: 'application/json',
      dataType: 'json',
      data: JSON.stringify(payload),
      success: function(resp) {
        $('#btn_schedule').prop('disabled', false);

        if (!resp || resp.success !== true) {
          var err = (resp && resp.error) ? resp.error : 'Scheduling fehlgeschlagen.';
          kcGrowl('ERROR: ' + err, 'error', 650, 8000);
          kcRefreshScheduleStatus();
          return;
        }

        var msg = 'Scheduled "' + resp.profile + '" für ' + resp.scheduled_for + '.';
        if (resp.job_id) msg += ' Job ' + resp.job_id + '.';

        kcGrowl(msg, 'success', 700, 8000);
        kcRefreshScheduleStatus();
      },
      error: function(xhr) {
        $('#btn_schedule').prop('disabled', false);
        var txt = (xhr && xhr.responseText) ? xhr.responseText : 'Scheduling fehlgeschlagen.';
        kcGrowl('ERROR: ' + txt, 'error', 700, 9000);
        kcRefreshScheduleStatus();
      }
    });
  } catch (e) {
    $('#btn_schedule').prop('disabled', false);
    kcGrowl('ERROR: ' + String(e), 'error', 650, 8000);
  }
}

// Initialize defaults + status polling
$(document).ready(function() {
  try {
    // Default date: tomorrow
    var dd = new Date();
    dd.setHours(0, 0, 0, 0);
    dd.setDate(dd.getDate() + 1);
    $('#schedule_date').val(kcFormatDateYYYYMMDD(dd));

    // Default time: next full hour
    var d = new Date();
    d.setMinutes(0, 0, 0);
    d.setHours(d.getHours() + 1);
    $('#schedule_time').val(kcPad2(d.getHours()) + ':' + kcPad2(d.getMinutes()));

    kcRefreshScheduleStatus();

    // Keep status fresh (e.g., after reboot or once the job ran)
    window.setInterval(kcRefreshScheduleStatus, 10000);
  } catch (e) {}
});
