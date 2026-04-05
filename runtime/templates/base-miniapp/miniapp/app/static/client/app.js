const role = "client";
window.setupPreviewBridge?.(role);

loadProfile();

async function loadProfile() {
  const response = await fetch(`/api/profiles/${role}`);
  const profile = await response.json();
  const avatar = document.getElementById("profile-avatar");
  const name = document.getElementById("profile-name");
  name.textContent = getDisplayName(profile, "Client profile");
  avatar.innerHTML = renderAvatar(profile.photo_url, getInitials(profile, "C"), "avatar", "avatar-fallback");
}

function getDisplayName(profile, fallback) {
  const fullName = `${profile.first_name || ""} ${profile.last_name || ""}`.trim();
  return fullName || fallback;
}

function getInitials(profile, fallback) {
  const fullName = getDisplayName(profile, fallback);
  const parts = fullName.split(/\s+/).filter(Boolean).slice(0, 2);
  if (parts.length === 0) {
    return fallback;
  }
  return parts.map((part) => part[0].toUpperCase()).join("");
}

function renderAvatar(photoUrl, fallbackText, imageClass, fallbackClass) {
  if (photoUrl) {
    return `<img class="${imageClass}" src="${photoUrl}" alt="" />`;
  }
  return `<div class="${fallbackClass}">${fallbackText}</div>`;
}
