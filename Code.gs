/**
 * PaperScout — Google Apps Script backend
 *
 * Paste this entire file into the Apps Script editor bound to your Google Sheet.
 * Deploy as a web app (Execute as: Me, Who has access: Anyone).
 *
 * Sheet columns (row 1 = headers, data starts row 2):
 *   A: Name
 *   B: Email
 *   C: Interests   (pipe-separated codes, e.g. "synthesis|moire|transport")
 *   D: TrackedPIs  (pipe-separated names, e.g. "Hone|Dean|Novoselov")
 *   E: FreeText    (open-ended interests)
 *   F: Active      (TRUE / FALSE)
 *   G: SignupDate  (ISO date)
 *   H: LastUpdated (ISO date)
 */

// ── Change this to a long random string and save it as a GitHub secret ───────
var ADMIN_KEY = PropertiesService.getScriptProperties().getProperty("ADMIN_KEY");

// ── Column indices (0-based) ──────────────────────────────────────────────────
var COL = { NAME: 0, EMAIL: 1, INTERESTS: 2, TRACKED_PIS: 3, FREE_TEXT: 4, ACTIVE: 5, SIGNUP: 6, UPDATED: 7 };

function getSheet() {
  return SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
}

function findRowByEmail(email) {
  var data = getSheet().getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    if (data[i][COL.EMAIL].toString().toLowerCase() === email.toLowerCase()) {
      return i + 1; // 1-based sheet row
    }
  }
  return -1;
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ── GET handler ───────────────────────────────────────────────────────────────

function doGet(e) {
  var action = (e.parameter.action || "").toLowerCase();

  // Lookup existing subscriber by email (used by the signup form)
  if (action === "lookup") {
    var email = e.parameter.email || "";
    if (!email) return jsonResponse({ error: "Missing email" });
    var row = findRowByEmail(email);
    if (row === -1) return jsonResponse({ found: false });
    var data = getSheet().getRange(row, 1, 1, 8).getValues()[0];
    return jsonResponse({
      found:       true,
      name:        data[COL.NAME],
      email:       data[COL.EMAIL],
      interests:   data[COL.INTERESTS]   ? data[COL.INTERESTS].split("|").filter(Boolean)   : [],
      tracked_pis: data[COL.TRACKED_PIS] ? data[COL.TRACKED_PIS].split("|").filter(Boolean) : [],
      free_text:   data[COL.FREE_TEXT],
      active:      data[COL.ACTIVE] === true || data[COL.ACTIVE] === "TRUE",
    });
  }

  // Return all active subscribers (called by paper_scout.py; protected by key)
  if (action === "subscribers") {
    if (!ADMIN_KEY || e.parameter.key !== ADMIN_KEY) {
      return jsonResponse({ error: "Unauthorized" });
    }
    var sheet   = getSheet();
    var allData = sheet.getDataRange().getValues();
    var subs    = [];
    for (var i = 1; i < allData.length; i++) {
      var row = allData[i];
      if (row[COL.EMAIL] && (row[COL.ACTIVE] === true || row[COL.ACTIVE] === "TRUE")) {
        subs.push({
          name:        row[COL.NAME],
          email:       row[COL.EMAIL],
          interests:   row[COL.INTERESTS]   ? row[COL.INTERESTS].split("|").filter(Boolean)   : [],
          tracked_pis: row[COL.TRACKED_PIS] ? row[COL.TRACKED_PIS].split("|").filter(Boolean) : [],
          free_text:   row[COL.FREE_TEXT],
          active:      true,
        });
      }
    }
    return jsonResponse({ subscribers: subs });
  }

  return jsonResponse({ error: "Unknown action" });
}

// ── POST handler ──────────────────────────────────────────────────────────────

function doPost(e) {
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return jsonResponse({ error: "Invalid JSON" });
  }

  var action = (body.action || "").toLowerCase();

  if (action === "signup" || action === "update") {
    return upsertSubscriber(body);
  }
  if (action === "unsubscribe") {
    return unsubscribeSubscriber(body.email || "");
  }

  return jsonResponse({ error: "Unknown action" });
}

// ── Signup / Update ───────────────────────────────────────────────────────────

function upsertSubscriber(data) {
  var email = (data.email || "").toLowerCase().trim();
  if (!email) return jsonResponse({ error: "Email required" });

  var name       = (data.name       || "").trim();
  var interests  = (data.interests  || []).join("|");
  var trackedPIs = (data.tracked_pis || []).join("|");
  var freeText   = (data.free_text  || "").trim();
  var now        = new Date().toISOString().slice(0, 10);
  var sheet      = getSheet();
  var row        = findRowByEmail(email);

  if (row === -1) {
    // New subscriber — append row
    sheet.appendRow([name, email, interests, trackedPIs, freeText, true, now, now]);
    sendWelcomeEmail(email, name);
    return jsonResponse({ ok: true, action: "created" });
  } else {
    // Existing subscriber — update in place (preserve signup date)
    var range = sheet.getRange(row, 1, 1, 8);
    var existing = range.getValues()[0];
    range.setValues([[
      name || existing[COL.NAME],
      email,
      interests,
      trackedPIs,
      freeText,
      true,
      existing[COL.SIGNUP],
      now,
    ]]);
    return jsonResponse({ ok: true, action: "updated" });
  }
}

// ── Unsubscribe ───────────────────────────────────────────────────────────────

function unsubscribeSubscriber(email) {
  email = (email || "").toLowerCase().trim();
  if (!email) return jsonResponse({ error: "Email required" });
  var row = findRowByEmail(email);
  if (row === -1) return jsonResponse({ ok: true, note: "Not found" });
  getSheet().getRange(row, COL.ACTIVE + 1).setValue(false);
  getSheet().getRange(row, COL.UPDATED + 1).setValue(new Date().toISOString().slice(0, 10));
  return jsonResponse({ ok: true, action: "unsubscribed" });
}

// ── Welcome email ─────────────────────────────────────────────────────────────

function sendWelcomeEmail(email, name) {
  var firstName = name ? name.split(" ")[0] : "there";
  var manageUrl = "https://mb5159.github.io/Hone-Lab-PaperScout/?email=" + encodeURIComponent(email);
  MailApp.sendEmail({
    to: email,
    subject: "You're subscribed to Hone Lab PaperScout",
    htmlBody:
      "<div style='font-family:Georgia,serif; max-width:520px; margin:0 auto; color:#1e1b4b;'>" +
      "<div style='background:linear-gradient(135deg,#1e1b4b,#4f46e5); padding:28px 32px; border-radius:12px 12px 0 0;'>" +
      "<div style='font-size:11px; color:#a5b4fc; letter-spacing:0.15em; text-transform:uppercase; margin-bottom:6px;'>Hone Lab · Columbia University</div>" +
      "<div style='font-size:22px; font-weight:700; color:#fff;'>PaperScout</div>" +
      "</div>" +
      "<div style='background:#fff; padding:28px 32px; border-radius:0 0 12px 12px; border:1px solid #e0e7ff; border-top:none;'>" +
      "<p style='font-size:16px; margin:0 0 12px;'>Hi " + firstName + ",</p>" +
      "<p style='font-size:14px; line-height:1.6; margin:0 0 16px;'>You're subscribed to the daily Hone Lab paper digest. " +
      "You'll get an email each morning with new papers matching the lab's interests.</p>" +
      "<p style='font-size:14px; line-height:1.6; margin:0 0 24px;'>You can update your interests or unsubscribe anytime:</p>" +
      "<a href='" + manageUrl + "' style='background:#4f46e5; color:#fff; padding:10px 20px; " +
      "border-radius:8px; text-decoration:none; font-size:13px; font-weight:600;'>Manage preferences</a>" +
      "<p style='font-size:12px; color:#94a3b8; margin-top:28px;'>Hone Lab PaperScout · Columbia University</p>" +
      "</div></div>",
  });
}

// ── One-time setup: write the admin key to script properties ─────────────────
// Run this function ONCE manually from the Apps Script editor after pasting in
// your chosen secret key (any long random string).
function setupAdminKey() {
  var key = "REPLACE_WITH_YOUR_SECRET_KEY";  // edit before running
  PropertiesService.getScriptProperties().setProperty("ADMIN_KEY", key);
  Logger.log("Admin key saved.");
}
