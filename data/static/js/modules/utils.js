// Logger conditionnel : n'affiche les logs que si le mode debug est actif
const isDebug = window.settings?.debug || false;
export const log = isDebug ? console.log.bind(console) : () => {};
export const debug = isDebug ? console.debug.bind(console) : () => {};

export function showError(message) {
    const errorDiv = document.getElementById('error');
    if (!errorDiv) {
        console.error('Element #error not found');
        console.error(message);
        return;
    }
    errorDiv.textContent = message;
    errorDiv.style.display = 'block';
    setTimeout(() => {
        errorDiv.style.display = 'none';
    }, 5000);
}

export function showSuccess(message) {
    const successDiv = document.getElementById('success');
    if (!successDiv) {
        console.error('Element #success not found');
        console.error(message);
        return;
    }
    successDiv.textContent = message;
    successDiv.style.display = 'block';
    setTimeout(() => {
        successDiv.style.display = 'none';
    }, 5000);
}

export function validateWebhookUrl(url) {
    if (!url) return false;
    try {
        new URL(url);
        return true;
    } catch {
        return false;
    }
}

export function handleApiError(error, defaultMessage = 'Une erreur est survenue') {
    console.error('API Error:', error);
    const message = error.response?.data?.error || error.message || defaultMessage;
    showError(message);
} 