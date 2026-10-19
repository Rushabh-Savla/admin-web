// Central Annotation Platform Dashboard Scripts
async function apiCall(url, method = "GET", body = null) {
  const options = {
    method,
    headers: {
      "Content-Type": "application/json",
    },
  };
  if (body) {
    options.body = JSON.stringify(body);
  }
  const res = await fetch(url, options);
  if (!res.ok) {
    const data = await res.json().catch(() => ({ detail: "Unknown error" }));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return res.json().catch(() => ({}));
}

async function triggerScan(datasetId) {
  try {
    await apiCall(`/api/v1/datasets/${datasetId}/scan`, "POST");
    alert("Catalog scan job enqueued. The page will reload shortly.");
    setTimeout(() => window.location.reload(), 1500);
  } catch (err) {
    alert("Scan failed: " + err.message);
  }
}

async function updateEligibleCount() {
  const datasetSelect = document.getElementById("wizard_dataset_id");
  const scopeInput = document.getElementById("wizard_directory_scope");
  const scopeTypeSelect = document.getElementById("wizard_scope_type");
  const countDisplay = document.getElementById("wizard_eligible_count");

  if (!datasetSelect || !datasetSelect.value) return;

  const datasetId = datasetSelect.value;
  const scope = encodeURIComponent(scopeInput ? scopeInput.value : "");
  const scopeType = scopeTypeSelect ? scopeTypeSelect.value : "RECURSIVE";

  try {
    const data = await apiCall(`/api/v1/datasets/${datasetId}/eligible-count?scope=${scope}&scope_type=${scopeType}`);
    if (countDisplay) {
      countDisplay.textContent = `${data.eligible_count} eligible images available`;
    }
  } catch (err) {
    if (countDisplay) countDisplay.textContent = "Error calculating count";
  }
}

async function approveBackgroundDecision(itemId, approved) {
  try {
    await apiCall(`/api/v1/submissions/reviews/${itemId}/approve-background`, "POST", { approved });
    alert(`Decision recorded (approved: ${approved}).`);
    window.location.reload();
  } catch (err) {
    alert("Error: " + err.message);
  }
}

async function retrySyncJob(jobId) {
  try {
    await apiCall(`/api/v1/jobs/${jobId}/retry`, "POST");
    alert("Job queued for retry.");
    window.location.reload();
  } catch (err) {
    alert("Error: " + err.message);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const datasetSelect = document.getElementById("wizard_dataset_id");
  if (datasetSelect && datasetSelect.value) {
    updateEligibleCount();
  }
});

// Dropdown Menu handler
function toggleDropdown(event, menuId) {
  if (event) {
    event.stopPropagation();
  }
  const targetMenu = document.getElementById(menuId);
  // Close any other open menus
  document.querySelectorAll(".dropdown-menu.show").forEach((m) => {
    if (m !== targetMenu) {
      m.classList.remove("show");
    }
  });
  if (targetMenu) {
    targetMenu.classList.toggle("show");
  }
}

// Global click handler to close open dropdowns
document.addEventListener("click", (e) => {
  if (!e.target.closest(".dropdown")) {
    document.querySelectorAll(".dropdown-menu.show").forEach((m) => {
      m.classList.remove("show");
    });
  }
});

// Clipboard copy helper
function copyPath(text) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => {
      alert("Copied to clipboard: " + text);
    }).catch(() => {
      prompt("Copy to clipboard:", text);
    });
  } else {
    prompt("Copy to clipboard:", text);
  }
}

// Real-time NAS Storage Health Monitoring
async function checkNasStatus() {
  const badge = document.getElementById("nas-status-badge");
  const banner = document.getElementById("nas-alert-banner");
  if (!badge) return;

  try {
    const res = await fetch("/api/v1/nas/status");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (data.connected) {
      badge.innerHTML = '<span style="color: #15803d; font-size: 0.75rem;">●</span> NAS: Connected';
      badge.style.background = "#dcfce7";
      badge.style.color = "#15803d";
      badge.title = data.message || "NAS storage is reachable";
      if (banner) banner.style.display = "none";
    } else {
      badge.innerHTML = '<span style="color: #b91c1c; font-size: 0.75rem;">●</span> NAS: Disconnected';
      badge.style.background = "#fee2e2";
      badge.style.color = "#b91c1c";
      badge.title = data.error || "Cannot reach NAS storage";
      if (banner) {
        banner.style.display = "block";
        const errSpan = document.getElementById("nas-alert-error");
        if (errSpan && data.error) errSpan.textContent = "(" + data.error + ")";
      }
    }
  } catch (err) {
    badge.innerHTML = '<span style="color: #b91c1c; font-size: 0.75rem;">●</span> NAS: Unreachable';
    badge.style.background = "#fee2e2";
    badge.style.color = "#b91c1c";
    if (banner) banner.style.display = "block";
  }
}

// Initialize NAS monitoring on DOM load and poll every 30 seconds
document.addEventListener("DOMContentLoaded", () => {
  checkNasStatus();
  setInterval(checkNasStatus, 30000);
  fetchNotifications();
  setInterval(fetchNotifications, 20000);
});

// Notifications & Master Action Triggers
function toggleNotificationDropdown(event) {
  if (event) event.stopPropagation();
  const dropdown = document.getElementById("notif-dropdown-menu");
  if (!dropdown) return;
  const isShown = dropdown.style.display === "block";
  dropdown.style.display = isShown ? "none" : "block";
  if (!isShown) {
    fetchNotifications();
  }
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".notif-wrapper")) {
    const dropdown = document.getElementById("notif-dropdown-menu");
    if (dropdown) dropdown.style.display = "none";
  }
});

async function fetchNotifications() {
  const badge = document.getElementById("notif-count-badge");
  const container = document.getElementById("notif-items-container");
  if (!badge || !container) return;

  try {
    const res = await fetch("/api/v1/notifications");
    if (!res.ok) return;
    const data = await res.json();
    const count = data.total_count || 0;

    if (count > 0) {
      badge.textContent = count;
      badge.style.display = "inline-block";
    } else {
      badge.style.display = "none";
    }

    let html = "";
    const completions = data.pending_completions || [];
    const reviews = data.pending_reviews || [];

    if (completions.length === 0 && reviews.length === 0) {
      html = `
        <div style="padding: 1.5rem 1rem; text-align: center; color: var(--text-muted);">
          <div style="font-size: 1.8rem; margin-bottom: 0.25rem;">✅</div>
          <strong style="color: var(--text); font-size: 0.9rem;">All Caught Up</strong>
          <p style="font-size: 0.82rem; margin: 0.25rem 0 0 0;">No pending assignment approvals or review queue tasks.</p>
        </div>
      `;
    } else {
      if (completions.length > 0) {
        html += `<div style="font-size: 0.78rem; text-transform: uppercase; font-weight: 700; color: #b45309; padding: 0.25rem 0.5rem; letter-spacing: 0.05em;">📥 Assignment Completion Approvals (${completions.length})</div>`;
        completions.forEach((c) => {
          html += `
            <div style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 0.65rem 0.85rem; margin-bottom: 0.5rem; font-size: 0.85rem;">
              <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.25rem;">
                <strong style="color: #92400e;">Assignment #${c.assignment_id}</strong>
                <span style="font-size: 0.75rem; color: #b45309;">${c.submitted_at || 'Just now'}</span>
              </div>
              <div style="color: var(--text); font-size: 0.83rem; margin-bottom: 0.5rem;">
                Annotator: <strong>${c.annotator_name}</strong> | Project: <strong>${c.project_title}</strong>
                <br><span style="color: var(--text-muted); font-size: 0.78rem;">${c.completed_count} / ${c.allocated_count} images (${c.object_count || 0} objects, ${c.background_count || 0} background)</span>
              </div>
              <div style="display: flex; gap: 0.4rem; justify-content: flex-end;">
                <button type="button" onclick="approveAssignmentDirect(${c.assignment_id})" class="btn" style="background: var(--success); color: #fff; padding: 0.2rem 0.6rem; font-size: 0.78rem; font-weight: 600;">
                  ✅ Approve
                </button>
                <button type="button" onclick="denyAssignmentDirect(${c.assignment_id})" class="btn btn-secondary" style="padding: 0.2rem 0.6rem; font-size: 0.78rem;">
                  ❌ Deny
                </button>
              </div>
            </div>
          `;
        });
      }

      if (reviews.length > 0) {
        html += `<div style="font-size: 0.78rem; text-transform: uppercase; font-weight: 700; color: #b91c1c; padding: 0.5rem 0.5rem 0.25rem 0.5rem; letter-spacing: 0.05em;">⚠️ Sync Errors &amp; Conflicts (${reviews.length})</div>`;
        html += `
          <div style="background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 0.65rem 0.85rem; margin-bottom: 0.5rem; font-size: 0.85rem;">
            <div style="color: #991b1b; font-size: 0.83rem; margin-bottom: 0.5rem;">
              <strong>${reviews.length} file(s)</strong> encountered upload or storage synchronization issues.
            </div>
            <div style="display: flex; justify-content: flex-end;">
              <a href="/dashboard?tab=reviews" class="btn btn-secondary" style="padding: 0.25rem 0.75rem; font-size: 0.78rem; font-weight: 600; text-decoration: none;">
                Inspect Errors &rarr;
              </a>
            </div>
          </div>
        `;
      }
    }
    container.innerHTML = html;
  } catch (e) {
    // silently catch network errors during polling
  }
}

async function approveAssignmentDirect(assignmentId) {
  try {
    const res = await fetch(`/api/v1/assignments/${assignmentId}/approve-completion`, { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      alert(`✅ Assignment #${assignmentId} completion approved! Task marked COMPLETED.`);
      fetchNotifications();
      if (window.location.pathname.startsWith("/dashboard")) {
        window.location.reload();
      }
    } else {
      alert("Approval error: " + (data.detail || "Failed to approve"));
    }
  } catch (err) {
    alert("Error: " + err.message);
  }
}

async function denyAssignmentDirect(assignmentId) {
  const reason = prompt("Enter reason for denying completion (requested rework):", "Needs revisions on labels");
  if (reason === null) return; // User cancelled prompt
  try {
    const res = await fetch(`/api/v1/assignments/${assignmentId}/deny-completion?reason=${encodeURIComponent(reason)}`, { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      alert(`Assignment #${assignmentId} completion denied. Returned to In Progress.`);
      fetchNotifications();
      if (window.location.pathname.startsWith("/dashboard")) {
        window.location.reload();
      }
    } else {
      alert("Denial error: " + (data.detail || "Failed to deny"));
    }
  } catch (err) {
    alert("Error: " + err.message);
  }
}



