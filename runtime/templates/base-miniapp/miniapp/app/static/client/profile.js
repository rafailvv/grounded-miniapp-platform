const role = "client";
const form = document.getElementById("profile-form");
const photoInput = document.getElementById("photo-input");
const saveButton = document.getElementById("save-button");
let currentPhotoUrl = null;

window.setupPreviewBridge?.(role);
loadProfile();
form.addEventListener("submit", saveProfile);
photoInput.addEventListener("change", onPhotoChange);

async function loadProfile() {
  const response = await fetch(`/api/profiles/${role}`);
  const profile = await response.json();
  currentPhotoUrl = profile.photo_url;
  form.elements.first_name.value = profile.first_name || "";
  form.elements.last_name.value = profile.last_name || "";
  form.elements.email.value = profile.email || "";
  form.elements.phone.value = profile.phone || "";
  renderPreview(profile);
}

async function saveProfile(event) {
  event.preventDefault();
  clearErrors();

  const payload = {
    first_name: form.elements.first_name.value.trim(),
    last_name: form.elements.last_name.value.trim(),
    email: form.elements.email.value.trim(),
    phone: form.elements.phone.value.trim(),
    photo_url: currentPhotoUrl,
  };

  let valid = true;
  if (!payload.email) {
    document.getElementById("email-error").textContent = "Enter an email address";
    valid = false;
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/i.test(payload.email)) {
    document.getElementById("email-error").textContent = "Enter a valid email address";
    valid = false;
  }

  if (!payload.phone) {
    document.getElementById("phone-error").textContent = "Enter a phone number";
    valid = false;
  }

  if (!valid) {
    return;
  }

  saveButton.textContent = "Saving...";
  const response = await fetch(`/api/profiles/${role}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const profile = await response.json();
  currentPhotoUrl = profile.photo_url;
  renderPreview(profile);
  saveButton.textContent = "Saved";
  window.setTimeout(() => {
    saveButton.textContent = "Save";
  }, 1200);
}

function onPhotoChange(event) {
  const [file] = event.target.files || [];
  if (!file) {
    return;
  }

  const reader = new FileReader();
  reader.onload = () => {
    currentPhotoUrl = typeof reader.result === "string" ? reader.result : null;
    renderPreview({
      first_name: form.elements.first_name.value.trim(),
      last_name: form.elements.last_name.value.trim(),
      photo_url: currentPhotoUrl,
    });
  };
  reader.readAsDataURL(file);
}

function renderPreview(profile) {
  document.getElementById("preview-name").textContent = getDisplayName(profile, "Client profile");
  document.getElementById("profile-photo").innerHTML = renderAvatar(profile.photo_url, "🙂");
}

function clearErrors() {
  document.getElementById("email-error").textContent = "";
  document.getElementById("phone-error").textContent = "";
}

function getDisplayName(profile, fallback) {
  const fullName = `${profile.first_name || ""} ${profile.last_name || ""}`.trim();
  return fullName || fallback;
}

function renderAvatar(photoUrl, fallbackText) {
  if (photoUrl) {
    return `<img class="avatar-large" src="${photoUrl}" alt="" />`;
  }
  return `<div class="avatar-large-fallback">${fallbackText}</div>`;
}
