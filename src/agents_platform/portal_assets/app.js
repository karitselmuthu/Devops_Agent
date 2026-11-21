const form = document.getElementById('repo-form');
const previewSection = document.getElementById('preview-section');
const statusSection = document.getElementById('status-section');
const formError = document.getElementById('form-error');
const launchError = document.getElementById('launch-error');
const statusError = document.getElementById('status-error');
let pendingRequest = null;
let workflowId = null;
let pollTimer = null;
let portalConfig = null;
let accessToken = sessionStorage.getItem('agents.accessToken');
let authReady;

function base64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomString() {
  return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

async function signIn() {
  const verifier = randomString() + randomString();
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  const state = randomString();
  sessionStorage.setItem('agents.pkceVerifier', verifier);
  sessionStorage.setItem('agents.oauthState', state);
  const target = new URL(`${portalConfig.cognito_domain}/oauth2/authorize`);
  target.search = new URLSearchParams({
    response_type: 'code', client_id: portalConfig.cognito_client_id,
    redirect_uri: `${location.origin}/`, scope: 'openid', state,
    code_challenge_method: 'S256', code_challenge: base64url(new Uint8Array(digest)),
  });
  location.assign(target.toString());
}

function renderAuth() {
  const button = document.getElementById('auth-button');
  const message = document.getElementById('auth-message');
  if (!portalConfig.authentication_required) return;
  button.hidden = false;
  const signedIn = Boolean(accessToken) && Number(sessionStorage.getItem('agents.tokenExpiry')) > Date.now();
  button.textContent = signedIn ? 'Sign out' : 'Sign in';
  form.hidden = !signedIn;
  document.getElementById('deploy-form').hidden = !signedIn;
  if (!signedIn) {
    accessToken = null;
    sessionStorage.removeItem('agents.accessToken');
    showError(message, 'Sign in to create or view workflows.');
  } else {
    clearError(message);
  }
}

async function initAuth() {
  const response = await fetch('/portal/config');
  if (!response.ok) throw new Error('Could not load sign-in settings.');
  portalConfig = await response.json();
  if (portalConfig.authentication_required && (!portalConfig.cognito_domain || !portalConfig.cognito_client_id)) {
    throw new Error('Cognito sign-in is not configured.');
  }
  const query = new URLSearchParams(location.search);
  if (portalConfig.authentication_required && query.has('code')) {
    const verifier = sessionStorage.getItem('agents.pkceVerifier');
    const state = sessionStorage.getItem('agents.oauthState');
    if (!verifier || !state || state !== query.get('state')) throw new Error('Sign-in state did not match. Please try again.');
    sessionStorage.removeItem('agents.pkceVerifier');
    sessionStorage.removeItem('agents.oauthState');
    const tokenResponse = await fetch(`${portalConfig.cognito_domain}/oauth2/token`, {
      method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code', client_id: portalConfig.cognito_client_id,
        code: query.get('code'), code_verifier: verifier, redirect_uri: `${location.origin}/`,
      }),
    });
    if (!tokenResponse.ok) throw new Error('Sign-in could not be completed. Please try again.');
    const tokens = await tokenResponse.json();
    accessToken = tokens.access_token;
    sessionStorage.setItem('agents.accessToken', accessToken);
    sessionStorage.setItem('agents.tokenExpiry', String(Date.now() + tokens.expires_in * 1000));
    history.replaceState({}, '', '/');
  }
  if (query.has('error')) {
    history.replaceState({}, '', '/');
    throw new Error('Sign-in was cancelled or denied. Please try again.');
  }
  renderAuth();
}

document.getElementById('auth-button').addEventListener('click', () => {
  if (accessToken) {
    accessToken = null;
    sessionStorage.removeItem('agents.accessToken');
    sessionStorage.removeItem('agents.tokenExpiry');
    sessionStorage.removeItem('agents.workflowId');
    sessionStorage.removeItem('agents.appId');
    if (pollTimer) clearInterval(pollTimer);
    statusSection.hidden = true;
    previewSection.hidden = true;
    renderAuth();
    const target = new URL(`${portalConfig.cognito_domain}/logout`);
    target.search = new URLSearchParams({client_id: portalConfig.cognito_client_id, logout_uri: `${location.origin}/`});
    location.assign(target.toString());
  } else {
    signIn().catch((error) => showError(document.getElementById('auth-message'), error.message));
  }
});

function showError(element, message) {
  element.textContent = message;
  element.hidden = false;
}

function clearError(element) {
  element.textContent = '';
  element.hidden = true;
}

async function api(path, options = {}) {
  await authReady;
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...(options.headers || {}),
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg).join('; ')
      : (detail || `Request failed (${response.status})`);
    throw new Error(response.status === 401 ? 'Sign in is required for this action.' : message);
  }
  return body;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearError(formError);
  if (!form.reportValidity()) return;
  const button = document.getElementById('preview-button');
  button.disabled = true;
  const draft = Object.fromEntries(new FormData(form).entries());
  try {
    const preview = await api('/portal/preview', {
      method: 'POST',
      body: JSON.stringify(draft),
    });
    pendingRequest = preview.plan.request;
    document.getElementById('preview-repository').textContent = draft.repository_url;
    document.getElementById('preview-branch').textContent = draft.ref;
    document.getElementById('preview-environment').textContent = draft.environment;
    document.getElementById('preview-next-step').textContent = preview.next_step;
    form.hidden = true;
    statusSection.hidden = true;
    previewSection.hidden = false;
    setStep(2);
    previewSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    showError(formError, error.message);
  } finally {
    button.disabled = false;
  }
});

document.getElementById('edit-button').addEventListener('click', () => {
  previewSection.hidden = true;
  form.hidden = false;
  setStep(1);
  document.getElementById('repository-url').focus();
});

document.getElementById('launch-button').addEventListener('click', async () => {
  clearError(launchError);
  const button = document.getElementById('launch-button');
  button.disabled = true;
  try {
    const plan = await api('/workflows', {
      method: 'POST',
      body: JSON.stringify(pendingRequest),
    });
    workflowId = plan.id;
    sessionStorage.setItem('agents.workflowId', workflowId);
    previewSection.hidden = true;
    statusSection.hidden = false;
    renderStatus(plan);
    statusSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    beginPolling();
  } catch (error) {
    showError(launchError, error.message);
  } finally {
    button.disabled = false;
  }
});

const steps = document.querySelectorAll('.steps .step');
// Highlight the current step (1-3); with done=true every step is marked complete.
function setStep(current, done = false) {
  steps.forEach((item, index) => {
    item.classList.toggle('active', !done && index + 1 === current);
    item.classList.toggle('done', done || index + 1 < current);
  });
}

function summarizeScan(task) {
  if (task.status !== 'COMPLETED' && task.status !== 'FAILED') return null;
  let report = null;
  try { report = JSON.parse(task.output); } catch (error) { /* not a scan report */ }
  if (report && report.outcome === 'clean') {
    return { kind: 'clean', text: `Scan clean: 0 findings (${report.scanners.join(', ')}). Nothing was deployed.` };
  }
  if (report && report.outcome === 'blocked') {
    const counts = {};
    report.findings.forEach((item) => { const level = (item.severity || 'unknown').toLowerCase(); counts[level] = (counts[level] || 0) + 1; });
    const detail = Object.entries(counts).map(([level, count]) => `${count} ${level}`).join(', ');
    return { kind: 'blocked', text: `Scan blocked: ${report.findings.length} finding${report.findings.length === 1 ? '' : 's'} (${detail}). Review the report below.` };
  }
  return { kind: 'error', text: 'The source check failed. See the details below.' };
}

function renderStatus(plan) {
  document.getElementById('workflow-id').textContent = plan.id;
  const task = plan.tasks[0];
  const pill = document.getElementById('task-status');
  pill.textContent = task.status.toLowerCase();
  pill.className = `status-pill ${task.status.toLowerCase()}`;
  const output = document.getElementById('task-output');
  output.textContent = task.output || '';
  output.hidden = !task.output;
  const summary = summarizeScan(task);
  const summaryBox = document.getElementById('task-summary');
  summaryBox.hidden = !summary;
  if (summary) { summaryBox.className = `scan-summary ${summary.kind}`; summaryBox.textContent = summary.text; }
  setStep(3, task.status === 'COMPLETED' || task.status === 'FAILED');
  if (task.status === 'COMPLETED' || task.status === 'FAILED') {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function refreshStatus() {
  if (!workflowId) return;
  clearError(statusError);
  try {
    renderStatus(await api(`/workflows/${encodeURIComponent(workflowId)}`));
  } catch (error) {
    showError(statusError, error.message);
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }
}

function beginPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refreshStatus, 5000);
}

document.getElementById('refresh-button').addEventListener('click', refreshStatus);
document.getElementById('new-button').addEventListener('click', () => {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  workflowId = null;
  pendingRequest = null;
  sessionStorage.removeItem('agents.workflowId');
  statusSection.hidden = true;
  previewSection.hidden = true;
  form.hidden = false;
  form.reset();
  setStep(1);
  document.getElementById('repository-url').focus();
});

authReady = initAuth().then(() => {
  workflowId = sessionStorage.getItem('agents.workflowId');
  if (workflowId && (!portalConfig.authentication_required || accessToken)) {
    form.hidden = true;
    statusSection.hidden = false;
    refreshStatus().then(() => { if (!statusError.hidden) return; beginPolling(); });
  }
  if (!portalConfig.authentication_required || accessToken) {
    loadAppList().then((plans) => {
      const active = plans?.find((plan) => ['APPROVED', 'BUILDING', 'DEPLOYING'].includes(plan.status));
      if (active) sessionStorage.setItem('agents.appId', active.id);
      const savedApp = sessionStorage.getItem('agents.appId');
      if (savedApp) showAppStatus(savedApp);
    });
  }
}).catch((error) => {
  form.hidden = true;
  showError(document.getElementById('auth-message'), error.message);
});

// "KEY=value" per line -> object; blank lines and #comments are ignored.
function parsePairs(text) {
  const result = {};
  (text || '').split('\n').forEach((raw, index) => {
    const line = raw.trim();
    if (!line || line.startsWith('#')) return;
    const at = line.indexOf('=');
    if (at < 1) throw new Error(`Line ${index + 1}: use KEY=value`);
    const key = line.slice(0, at).trim();
    if (Object.hasOwn(result, key)) throw new Error(`Line ${index + 1}: ${key} is listed twice`);
    result[key] = line.slice(at + 1).trim();
  });
  return result;
}

const deployForm = document.getElementById('deploy-form');
const deployPlanSection = document.getElementById('deploy-plan');
const deployStatusSection = document.getElementById('deploy-status');
let currentDeploymentId = null;
let appPollTimer = null;
let appStatusRequest = 0;

function updateLatestAppButton() {
  const savedApp = sessionStorage.getItem('agents.appId');
  document.getElementById('latest-app-button').hidden = !savedApp || savedApp === currentDeploymentId;
}

deployForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const errorElement = document.getElementById('deploy-form-error');
  clearError(errorElement);
  if (!deployForm.reportValidity()) return;
  const button = document.getElementById('deploy-plan-button');
  button.disabled = true;
  const draft = Object.fromEntries(new FormData(deployForm).entries());
  if (!draft.ref) delete draft.ref;
  if (!draft.name) delete draft.name;
  if (!draft.health_path) delete draft.health_path;
  if (!draft.dockerfile_path) delete draft.dockerfile_path;
  try {
    const settings = parsePairs(draft.environment_text);
    const secrets = parsePairs(draft.secrets_text);
    if (Object.keys(settings).length) draft.environment_variables = settings;
    if (Object.keys(secrets).length) draft.secret_refs = secrets;
  } catch (error) {
    showError(errorElement, error.message);
    button.disabled = false;
    return;
  }
  delete draft.environment_text;
  delete draft.secrets_text;
  draft.port = draft.port ? Number(draft.port) : null;
  try {
    const plan = await api('/apps/plans', { method: 'POST', body: JSON.stringify(draft) });
    currentDeploymentId = plan.id;
    appStatusRequest += 1;
    if (appPollTimer) clearInterval(appPollTimer);
    appPollTimer = null;
    sessionStorage.setItem('agents.appId', plan.id);
    updateLatestAppButton();
    renderAppPlan(plan);
    deployStatusSection.hidden = true;
    deployPlanSection.hidden = false;
    deployPlanSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    loadAppList();
  } catch (error) {
    showError(errorElement, error.message);
  } finally {
    button.disabled = false;
  }
});

function renderAppPlan(plan) {
  document.getElementById('deploy-plan-repository').textContent = plan.request.repository_url;
  document.getElementById('deploy-plan-commit').textContent = plan.source_commit;
  document.getElementById('deploy-plan-name').textContent = plan.request.name;
  const sam = plan.deployment_kind === 'sam_lambda';
  document.getElementById('deploy-plan-kind').textContent = sam ? 'Python SAM / Lambda' : 'Dockerfile / ECS';
  document.getElementById('deploy-plan-port').textContent = sam ? 'Not applicable' : String(plan.request.port);
  document.getElementById('deploy-plan-health-path').textContent = sam ? 'Lambda smoke check' : plan.request.health_path;
  document.getElementById('deploy-plan-stack').textContent = plan.stack_name;
  document.getElementById('deploy-billing-note').textContent = sam
    ? 'CodeBuild bills during packaging. Lambda, DynamoDB, Bedrock, and any configured S3 Vectors bill for usage. The existing platform continues billing while deployed. Approval starts the build and creates IAM roles, Lambda, and a DynamoDB table.'
    : 'CodeBuild bills during the build. Fargate bills while the app task runs. CloudFront bills for usage. The existing platform continues billing while deployed. Approval starts resource creation.';
  const prerequisites = document.getElementById('deploy-prerequisites');
  prerequisites.replaceChildren();
  for (const requirement of plan.prerequisites || []) {
    const item = document.createElement('li');
    item.textContent = requirement;
    prerequisites.append(item);
  }
  const list = document.getElementById('deploy-resources');
  list.replaceChildren();
  for (const resource of plan.resources) {
    const item = document.createElement('li');
    const service = document.createElement('strong');
    service.textContent = resource.service;
    const purpose = document.createElement('span');
    purpose.textContent = `${resource.name}: ${resource.purpose}`;
    item.append(service, purpose);
    if (resource.billed_while_running) {
      const billing = document.createElement('small');
      billing.textContent = 'Billable';
      item.append(billing);
    }
    list.append(item);
  }
  const approve = document.getElementById('deploy-approve-button');
  approve.hidden = !portalConfig.app_deployment_enabled || plan.status !== 'PLANNED';
  const errorElement = document.getElementById('deploy-plan-error');
  clearError(errorElement);
  if (!portalConfig.app_deployment_enabled) {
    showError(errorElement, 'Local mode supports planning only. Deploy the Agents Platform to AWS to enable approval and builds.');
  }
}

document.getElementById('deploy-edit-button').addEventListener('click', () => {
  deployPlanSection.hidden = true;
  deployForm.hidden = false;
  document.getElementById('deploy-repository').focus();
});

document.getElementById('deploy-approve-button').addEventListener('click', async () => {
  const errorElement = document.getElementById('deploy-plan-error');
  clearError(errorElement);
  const button = document.getElementById('deploy-approve-button');
  button.disabled = true;
  try {
    const plan = await api(`/apps/${encodeURIComponent(currentDeploymentId)}/approve`, { method: 'POST' });
    renderAppStatus(plan);
    deployPlanSection.hidden = true;
    deployStatusSection.hidden = false;
    beginAppPolling();
    loadAppList();
  } catch (error) {
    showError(errorElement, error.message);
  } finally {
    button.disabled = false;
  }
});

function renderAppStatus(plan) {
  currentDeploymentId = plan.id;
  updateLatestAppButton();
  document.getElementById('deploy-status-name').textContent = plan.request.name;
  const status = document.getElementById('deploy-status-value');
  status.textContent = plan.status.toLowerCase();
  status.className = `status-pill ${plan.status.toLowerCase()}`;
  document.getElementById('deploy-build-id').textContent = plan.build_id || 'Pending';
  const urlField = document.getElementById('deploy-url');
  urlField.replaceChildren();
  if (plan.service_url && plan.service_url.startsWith('https://')) {
    const link = document.createElement('a');
    link.href = plan.service_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = plan.service_url;
    urlField.append(link);
  } else {
    urlField.textContent = 'Pending';
  }
  document.getElementById('deploy-access-note').textContent = plan.access_mode === 'aws_iam'
    ? 'This Lambda Function URL requires an AWS SigV4-signed request; opening it directly in a browser returns 403.'
    : '';
  const errorElement = document.getElementById('deploy-status-error');
  if (plan.error) showError(errorElement, plan.error); else clearError(errorElement);
  document.getElementById('deploy-delete-button').hidden = !portalConfig.app_deployment_enabled || plan.status === 'DELETED';
  if (['RUNNING', 'FAILED', 'DELETED'].includes(plan.status) && appPollTimer) {
    clearInterval(appPollTimer);
    appPollTimer = null;
  }
}

async function showAppStatus(id, reveal = false) {
  const request = ++appStatusRequest;
  if (currentDeploymentId !== id || reveal) {
    if (appPollTimer) clearInterval(appPollTimer);
    appPollTimer = null;
  }
  currentDeploymentId = id;
  updateLatestAppButton();
  if (reveal) {
    deployPlanSection.hidden = true;
    deployStatusSection.hidden = false;
    const status = document.getElementById('deploy-status-value');
    status.textContent = 'loading';
    status.className = 'status-pill';
    document.getElementById('deploy-delete-button').hidden = true;
    clearError(document.getElementById('deploy-status-error'));
    deployStatusSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  try {
    const plan = await api(`/apps/${encodeURIComponent(id)}`);
    if (request !== appStatusRequest) return;
    renderAppStatus(plan);
    deployPlanSection.hidden = true;
    deployStatusSection.hidden = false;
    if (['APPROVED', 'BUILDING', 'DEPLOYING', 'DELETING'].includes(plan.status)) beginAppPolling();
  } catch (error) {
    if (request !== appStatusRequest) return;
    if (reveal) document.getElementById('deploy-status-value').textContent = 'unavailable';
    showError(document.getElementById('deploy-status-error'), error.message);
  }
}

function beginAppPolling() {
  if (appPollTimer) clearInterval(appPollTimer);
  appPollTimer = setInterval(() => showAppStatus(currentDeploymentId), 5000);
}

document.getElementById('deploy-refresh-button').addEventListener('click', () => {
  if (currentDeploymentId) showAppStatus(currentDeploymentId);
});

document.getElementById('latest-app-button').addEventListener('click', () => {
  const savedApp = sessionStorage.getItem('agents.appId');
  if (savedApp) showAppStatus(savedApp, true);
});

document.getElementById('deploy-delete-button').addEventListener('click', async () => {
  if (!currentDeploymentId) return;
  const appName = document.getElementById('deploy-status-name').textContent;
  if (!window.confirm(`Delete AWS resources for ${appName}? This removes the running service and its image repository.`)) return;
  const errorElement = document.getElementById('deploy-status-error');
  clearError(errorElement);
  try {
    const plan = await api(`/apps/${encodeURIComponent(currentDeploymentId)}`, { method: 'DELETE' });
    renderAppStatus(plan);
    beginAppPolling();
    loadAppList();
  } catch (error) {
    showError(errorElement, error.message);
  }
});

async function loadAppList() {
  try {
    const plans = await api('/apps');
    const list = document.getElementById('app-list');
    list.replaceChildren();
    if (!plans.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-list';
      empty.textContent = 'No application plans yet.';
      list.append(empty);
      return;
    }
    for (const plan of plans) {
      const item = document.createElement('div');
      item.className = 'app-item';
      const details = document.createElement('div');
      const name = document.createElement('strong');
      name.textContent = plan.request.name;
      const meta = document.createElement('small');
      meta.textContent = `${plan.status} · ${plan.request.environment} · ${plan.request.ref}`;
      details.append(name, meta);
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = 'View';
      button.addEventListener('click', () => showAppStatus(plan.id, true));
      item.append(details, button);
      list.append(item);
    }
    return plans;
  } catch (error) {
    const list = document.getElementById('app-list');
    list.textContent = error.message;
  }
}

document.getElementById('apps-refresh-button').addEventListener('click', loadAppList);
