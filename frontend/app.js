const API_BASE_URL = "http://127.0.0.1:8000/api";

async function loadDashboard() {
  const statusElement = document.querySelector("#status");
  const dashboardElement = document.querySelector("#dashboard");

  try {
    const response = await fetch(`${API_BASE_URL}/dashboard`);
    if (!response.ok) throw new Error(`Request failed: ${response.status}`);

    const data = await response.json();
    statusElement.textContent = "Backend connected";
    dashboardElement.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    statusElement.textContent = "Backend unavailable";
    dashboardElement.textContent = error.message;
  }
}

loadDashboard();
