const state = {
  user: null,
  users: [],
  dashboard: { kpis: {}, portfolio_by_type: [], recent_movements: [], recent_dividends: [] },
  accounts: [],
  applications: [],
  movements: [],
  dividends: [],
  earnings: [],
  snapshots: [],
  imports: [],
  report: { month: '', totals: {}, portfolio_by_type: [], portfolio_by_account: [], monthly_rows: [], filters: {} }
};

const typeOptions = [
  'Renda Fixa',
  'Fundo de Investimento',
  'COE',
  'Tesouro',
  'Previdência',
  'Multimercado',
  'Poupança',
  'Outros'
];

const $ = (id) => document.getElementById(id);
const today = new Date().toISOString().slice(0, 10);
const currentMonth = new Date().toISOString().slice(0, 7);
let pendingRestoreFile = null;

function formatCurrency(value) {
  return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(Number(value || 0));
}

function formatPercent(value, min = 2, max = 4) {
  return Number(value || 0).toLocaleString('pt-BR', { minimumFractionDigits: min, maximumFractionDigits: max });
}

function formatDate(value) {
  if (!value) return '-';
  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    const [year, month, day] = text.split('-');
    return `${day}/${month}/${year}`;
  }
  const parsed = new Date(text.replace(' ', 'T'));
  if (Number.isNaN(parsed.getTime())) return escapeHtml(text);
  return parsed.toLocaleDateString('pt-BR');
}

function formatMonthLabel(value) {
  if (!value) return '-';
  const text = String(value).slice(0, 7);
  if (!/^\d{4}-\d{2}$/.test(text)) return text;
  const [year, month] = text.split('-');
  return `${month}/${year}`;
}

function monthOf(item, dateKey = 'date', competenceKey = 'competence') {
  return String(item?.[competenceKey] || item?.[dateKey] || '').slice(0, 7);
}

function getProjectedBalanceForApplication(applicationId, competence = currentMonth, excludeEarningId = null) {
  const app = findById(state.applications, applicationId);
  if (!app) return 0;

  const latestSnapshot = [...state.snapshots]
    .filter((item) => String(item.application_id) === String(applicationId) && String(item.ref_month || '') <= competence)
    .sort((a, b) => String(b.ref_month || '').localeCompare(String(a.ref_month || '')) || Number(b.id || 0) - Number(a.id || 0))[0];

  if (latestSnapshot) return Number(latestSnapshot.balance || 0);

  let total = Number(app.initial_value || 0);
  state.movements.forEach((item) => {
    if (String(item.application_id) !== String(applicationId)) return;
    if (monthOf(item) > competence) return;
    total += item.kind === 'resgate' ? -Number(item.amount || 0) : Number(item.amount || 0);
  });
  state.dividends.forEach((item) => {
    if (String(item.application_id) !== String(applicationId)) return;
    if (monthOf(item, 'payment_date') > competence) return;
    total += Number(item.net_amount || 0);
  });
  state.earnings.forEach((item) => {
    if (String(item.application_id) !== String(applicationId)) return;
    if (excludeEarningId && String(item.id) === String(excludeEarningId)) return;
    if (monthOf(item, 'payment_date') > competence) return;
    total += Number(item.amount || 0);
  });
  return total;
}

function updateEarningHint(message, cssClass = 'summary-box muted') {
  $('earningCalcHint').className = cssClass;
  $('earningCalcHint').innerHTML = message;
}

function recalculateEarningFields() {
  const previousBalance = Number($('earningPreviousBalance').value || 0);
  const currentBalance = Number($('earningCurrentBalance').value || 0);

  if (previousBalance > 0 && currentBalance > 0) {
    const amount = Number((currentBalance - previousBalance).toFixed(2));
    const percent = previousBalance > 0 ? Number((amount / previousBalance * 100).toFixed(4)) : 0;
    $('earningAmount').value = amount.toFixed(2);
    $('earningPercent').value = percent.toFixed(4);
    const mood = amount >= 0 ? 'summary-box' : 'summary-box muted';
    updateEarningHint(`Saldo anterior de <strong>${formatCurrency(previousBalance)}</strong> para saldo atual de <strong>${formatCurrency(currentBalance)}</strong> gera rendimento de <strong>${formatCurrency(amount)}</strong>, equivalente a <strong>${formatPercent(percent)}%</strong>.`, mood);
  } else {
    $('earningAmount').value = '';
    $('earningPercent').value = '';
    updateEarningHint('Informe o saldo anterior e o saldo atual. O sistema calculará automaticamente o rendimento em R$ e a rentabilidade do período.', 'summary-box muted');
  }
}

function useProjectedBalanceForEarning() {
  const applicationId = $('earningApplication').value;
  if (!applicationId) {
    showFlash('Selecione uma aplicação primeiro.', 'error');
    return;
  }
  const projected = getProjectedBalanceForApplication(applicationId, $('earningCompetence').value || currentMonth, $('earningId').value || null);
  if (!projected) {
    showFlash('Não encontrei saldo projetado para essa aplicação. Informe manualmente.', 'error');
    return;
  }
  $('earningPreviousBalance').value = Number(projected).toFixed(2);
  recalculateEarningFields();
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function showFlash(message, type = 'success') {
  const flash = $('flash');
  flash.className = `show ${type}`;
  flash.textContent = message;
  clearTimeout(showFlash.timer);
  showFlash.timer = setTimeout(() => {
    flash.className = '';
    flash.textContent = '';
  }, 4500);
}

async function api(path, options = {}) {
  const config = {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
  };
  if (config.body instanceof FormData) {
    delete config.headers['Content-Type'];
  }
  const response = await fetch(path, config);
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (response.status === 401) {
    $('loginOverlay').classList.remove('hidden');
    throw new Error('Sessão expirada. Faça login novamente.');
  }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || data.message || 'Não foi possível concluir a ação.');
  }
  return data;
}

function setActiveSection(sectionId) {
  document.querySelectorAll('.page').forEach((section) => {
    section.classList.toggle('active', section.id === sectionId);
  });
  document.querySelectorAll('#menuNav button').forEach((button) => {
    button.classList.toggle('active', button.dataset.section === sectionId);
  });
}

function fillSelect(selectId, items, valueKey = 'id', labelFn = (item) => item.name) {
  const select = $(selectId);
  const current = select.value;
  select.innerHTML = items.length
    ? items.map((item) => `<option value="${item[valueKey]}">${escapeHtml(labelFn(item))}</option>`).join('')
    : '<option value="">Nenhum registro disponível</option>';
  if (items.some((item) => String(item[valueKey]) === String(current))) {
    select.value = current;
  }
}

function fillStaticTypes() {
  const select = $('applicationType');
  select.innerHTML = typeOptions.map((type) => `<option value="${type}">${type}</option>`).join('');
}

function updateSelects() {
  fillSelect('applicationAccount', state.accounts, 'id', (item) => `${item.name} · ${item.institution}`);
  const appLabel = (item) => `${item.name} · ${item.account_name}`;
  fillSelect('movementApplication', state.applications, 'id', appLabel);
  fillSelect('dividendApplication', state.applications, 'id', appLabel);
  fillSelect('earningApplication', state.applications, 'id', appLabel);
}

function fillFilterSelect(selectId, items, labelFn, currentValue = '') {
  const select = $(selectId);
  if (!select) return;
  const options = ['<option value="">Todos</option>'].concat(items.map((item) => `<option value="${item.value}">${escapeHtml(labelFn(item))}</option>`));
  select.innerHTML = options.join('');
  select.value = currentValue && options.join('').includes(`value="${currentValue}"`) ? String(currentValue) : '';
}

function updateReportFilterOptions() {
  const filters = state.report?.filters || {};
  const selectedAccount = String(filters.account_id || $('reportAccountFilter')?.value || '');
  const selectedApplication = String(filters.application_id || $('reportApplicationFilter')?.value || '');
  const selectedType = String(filters.app_type || $('reportTypeFilter')?.value || '');
  fillFilterSelect('reportAccountFilter', state.accounts.map((item) => ({ value: item.id, label: `${item.name} · ${item.institution}` })), (item) => item.label, selectedAccount);
  const filteredApps = selectedAccount ? state.applications.filter((item) => String(item.account_id) === selectedAccount) : state.applications;
  fillFilterSelect('reportApplicationFilter', filteredApps.map((item) => ({ value: item.id, label: `${item.name} · ${item.account_name}` })), (item) => item.label, selectedApplication);
  fillFilterSelect('reportTypeFilter', typeOptions.map((item) => ({ value: item, label: item })), (item) => item.label, selectedType);
}

function buildReportQueryString() {
  const params = new URLSearchParams();
  params.set('month', $('reportMonth').value || currentMonth);
  if ($('reportAccountFilter')?.value) params.set('account_id', $('reportAccountFilter').value);
  if ($('reportApplicationFilter')?.value) params.set('application_id', $('reportApplicationFilter').value);
  if ($('reportTypeFilter')?.value) params.set('app_type', $('reportTypeFilter').value);
  return params.toString();
}

function metricCard(label, value, foot = '') {
  return `<div class="card metric-card"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-foot">${foot}</div></div>`;
}

function barList(items, emptyText = 'Sem dados para exibir.') {
  if (!items.length) return `<div class="table-empty">${emptyText}</div>`;
  return items.map((item) => `
    <div class="bar-item">
      <strong>${escapeHtml(item.type || item.account_name || item.label || '-')}</strong>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, Number(item.percent || 0))}%"></div></div>
      <span>${formatCurrency(item.value)}</span>
    </div>
  `).join('');
}

function roleLabel(role) {
  return role === 'admin' ? 'Administrador' : 'Usuário';
}

function formatDateTime(value) {
  if (!value) return '-';
  const raw = String(value).replace(' ', 'T');
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return escapeHtml(String(value));
  return parsed.toLocaleString('pt-BR');
}

function renderDashboard() {
  const kpis = state.dashboard.kpis || {};
  $('dashboardCards').innerHTML = [
    metricCard('Patrimônio atual', formatCurrency(kpis.patrimonio), 'Último snapshot ou valor base'),
    metricCard('Contas', String(kpis.accounts || 0), 'Cadastros ativos'),
    metricCard('Aplicações', String(kpis.applications || 0), 'Cadastros ativos'),
    metricCard('Aportes', formatCurrency(kpis.aportes), 'Total lançado'),
    metricCard('Resgates', formatCurrency(kpis.resgates), 'Total lançado'),
    metricCard('Dividendos líquidos', formatCurrency(kpis.dividendos), 'Acumulado'),
    metricCard('Rendimentos', formatCurrency(kpis.rendimentos), 'Manuais + importados'),
    metricCard('Importações', String(kpis.imports || 0), 'Execuções registradas')
  ].join('');

  $('portfolioBars').innerHTML = barList(state.dashboard.portfolio_by_type || [], 'Nenhuma aplicação cadastrada.');

  $('recentMovements').innerHTML = (state.dashboard.recent_movements || []).length
    ? state.dashboard.recent_movements.map((item) => `
        <tr>
          <td>${formatDate(item.date || '-')}</td>
          <td>${escapeHtml(item.application_name || '-')}</td>
          <td><span class="badge ${item.kind === 'resgate' ? 'resgate' : 'aporte'}">${escapeHtml(item.kind || '-')}</span></td>
          <td>${formatCurrency(item.amount)}</td>
        </tr>
      `).join('')
    : '<tr><td colspan="4" class="table-empty">Nenhum lançamento de aporte ou resgate.</td></tr>';

  $('recentDividends').innerHTML = (state.dashboard.recent_dividends || []).length
    ? state.dashboard.recent_dividends.map((item) => `
        <tr>
          <td>${formatDate(item.payment_date || '-')}</td>
          <td>${escapeHtml(item.application_name || '-')}</td>
          <td>${formatCurrency(item.net_amount)}</td>
        </tr>
      `).join('')
    : '<tr><td colspan="3" class="table-empty">Nenhum dividendo lançado.</td></tr>';
}

function renderAccounts() {
  $('accountsCountLabel').textContent = `${state.accounts.length} conta(s)`;
  $('accountsTable').innerHTML = state.accounts.length
    ? state.accounts.map((item) => `
        <tr>
          <td><strong>${escapeHtml(item.name)}</strong><br><small>${escapeHtml(item.notes || '')}</small></td>
          <td>${escapeHtml(item.institution)}</td>
          <td>${escapeHtml(item.currency)}</td>
          <td>${item.applications_count || 0}</td>
          <td>
            <div class="table-actions">
              <button class="secondary" onclick="editAccount(${item.id})">Editar</button>
              <button class="danger" onclick="deleteAccount(${item.id})">Excluir</button>
            </div>
          </td>
        </tr>
      `).join('')
    : '<tr><td colspan="5" class="table-empty">Nenhuma conta cadastrada.</td></tr>';
}

function renderApplications() {
  $('applicationsCountLabel').textContent = `${state.applications.length} aplicação(ões)`;
  $('applicationsTable').innerHTML = state.applications.length
    ? state.applications.map((item) => `
        <tr>
          <td><strong>${escapeHtml(item.name)}</strong><br><small>${escapeHtml(item.code || '')}</small></td>
          <td>${escapeHtml(item.account_name)}</td>
          <td>${escapeHtml(item.type)}</td>
          <td>${formatCurrency(item.initial_value)}</td>
          <td>
            <div class="table-actions">
              <button class="secondary" onclick="editApplication(${item.id})">Editar</button>
              <button class="danger" onclick="deleteApplication(${item.id})">Excluir</button>
            </div>
          </td>
        </tr>
      `).join('')
    : '<tr><td colspan="5" class="table-empty">Nenhuma aplicação cadastrada.</td></tr>';
}

function renderMovements() {
  $('movementsCountLabel').textContent = `${state.movements.length} lançamento(s)`;
  $('movementsTable').innerHTML = state.movements.length
    ? state.movements.map((item) => `
        <tr>
          <td>${formatDate(item.date)}</td>
          <td><strong>${escapeHtml(item.application_name)}</strong><br><small>${escapeHtml(item.account_name)}</small></td>
          <td><span class="badge ${item.kind === 'resgate' ? 'resgate' : 'aporte'}">${escapeHtml(item.kind)}</span></td>
          <td>${formatCurrency(item.amount)}</td>
          <td>
            <div class="table-actions">
              <button class="secondary" onclick="editMovement(${item.id})">Editar</button>
              <button class="danger" onclick="deleteMovement(${item.id})">Excluir</button>
            </div>
          </td>
        </tr>
      `).join('')
    : '<tr><td colspan="5" class="table-empty">Nenhum lançamento de aporte ou resgate.</td></tr>';
}

function renderDividends() {
  $('dividendsCountLabel').textContent = `${state.dividends.length} dividendo(s)`;
  $('dividendsTable').innerHTML = state.dividends.length
    ? state.dividends.map((item) => `
        <tr>
          <td>${formatDate(item.payment_date)}</td>
          <td><strong>${escapeHtml(item.application_name)}</strong><br><small>${escapeHtml(item.account_name)}</small></td>
          <td>${formatCurrency(item.gross_amount)}</td>
          <td>${formatCurrency(item.net_amount)}</td>
          <td>
            <div class="table-actions">
              <button class="secondary" onclick="editDividend(${item.id})">Editar</button>
              <button class="danger" onclick="deleteDividend(${item.id})">Excluir</button>
            </div>
          </td>
        </tr>
      `).join('')
    : '<tr><td colspan="5" class="table-empty">Nenhum dividendo cadastrado.</td></tr>';
}

function renderEarnings() {
  $('earningsCountLabel').textContent = `${state.earnings.length} rendimento(s)`;
  $('earningsTable').innerHTML = state.earnings.length
    ? state.earnings.map((item) => {
        const currentBalance = Number(item.current_balance || 0);
        const percent = Number(item.percent || 0) || (currentBalance > 0 ? Number((Number(item.amount || 0) / currentBalance * 100).toFixed(4)) : 0);
        return `
        <tr>
          <td>${formatDate(item.payment_date)}</td>
          <td><strong>${escapeHtml(item.application_name)}</strong><br><small>${escapeHtml(item.account_name)}</small></td>
          <td>${formatCurrency(item.amount)}</td>
          <td>${percent ? `${formatPercent(percent)}%` : '-'}</td>
          <td>${currentBalance ? formatCurrency(currentBalance) : '-'}</td>
          <td>${item.origin_key ? '<span class="badge importado">Importado</span>' : 'Manual'}</td>
          <td>
            <div class="table-actions">
              <button class="secondary" onclick="editEarning(${item.id})">Editar</button>
              <button class="danger" onclick="deleteEarning(${item.id})">Excluir</button>
            </div>
          </td>
        </tr>
      `;
      }).join('')
    : '<tr><td colspan="7" class="table-empty">Nenhum rendimento cadastrado.</td></tr>';
}

function renderImportSection() {
  $('snapshotsTable').innerHTML = state.snapshots.length
    ? state.snapshots.slice(0, 20).map((item) => `
        <tr>
          <td>${formatMonthLabel(item.ref_month)}</td>
          <td>${escapeHtml(item.account_name)}</td>
          <td>${escapeHtml(item.application_name)}</td>
          <td>${formatCurrency(item.balance)}</td>
          <td>${escapeHtml(item.source)}</td>
        </tr>
      `).join('')
    : '<tr><td colspan="5" class="table-empty">Nenhum snapshot importado.</td></tr>';

  $('importsTable').innerHTML = state.imports.length
    ? state.imports.map((item) => {
        let summary = '-';
        try {
          const parsed = JSON.parse(item.summary_json || '{}');
          summary = `${parsed.snapshots_imported || 0} snapshots, ${parsed.earnings_imported || 0} rendimentos, ${parsed.applications_created || 0} aplicações.`;
        } catch (error) {
          summary = item.summary_json || '-';
        }
        return `
          <tr>
            <td>${formatDateTime(item.imported_at)}</td>
            <td>${escapeHtml(item.filename)}</td>
            <td>${escapeHtml(summary)}</td>
          </tr>
        `;
      }).join('')
    : '<tr><td colspan="3" class="table-empty">Nenhum log de importação.</td></tr>';
}

function renderUsers() {
  if (!$('usersTable') || !$('usersCountLabel')) return;
  $('usersCountLabel').textContent = `${state.users.length} usuário(s)`;
  if (!(state.user && state.user.is_admin)) {
    $('usersTable').innerHTML = '<tr><td colspan="5" class="table-empty">Somente administradores podem gerenciar usuários.</td></tr>';
    return;
  }
  $('usersTable').innerHTML = state.users.length
    ? state.users.map((item) => `
        <tr>
          <td><strong>${escapeHtml(item.name)}</strong><br><small>${escapeHtml(item.email)}</small></td>
          <td><span class="badge ${item.role === 'admin' ? 'admin' : 'user'}">${roleLabel(item.role)}</span></td>
          <td><span class="badge ${item.active ? 'active' : 'inactive'}">${item.active ? 'Ativo' : 'Inativo'}</span></td>
          <td>${formatDateTime(item.created_at)}</td>
          <td>
            <div class="table-actions">
              <button class="secondary" onclick="editUser(${item.id})">Editar</button>
              <button class="secondary" onclick="toggleUserStatus(${item.id})">${item.active ? 'Desativar' : 'Ativar'}</button>
              <button onclick="resetUserPasswordPrompt(${item.id})">Nova senha</button>
            </div>
          </td>
        </tr>
      `).join('')
    : '<tr><td colspan="5" class="table-empty">Nenhum usuário cadastrado.</td></tr>';
}

function renderAdminStats() {
  if (!$('adminStats')) return;
  const user = state.user || {};
  const isAdmin = Boolean(user.is_admin || user.role === 'admin');
  const totalUsers = state.users.length;
  const activeUsers = state.users.filter((item) => item.active).length;
  const adminUsers = state.users.filter((item) => item.role === 'admin' && item.active).length;
  const inactiveUsers = state.users.filter((item) => !item.active).length;

  const cards = isAdmin
    ? [
        `<div class="card metric-card admin-highlight"><div class="metric-label">Usuários cadastrados</div><div class="metric-value">${totalUsers}</div><div class="metric-foot">${activeUsers} ativo(s) no sistema</div></div>`,
        `<div class="card metric-card admin-highlight"><div class="metric-label">Administradores ativos</div><div class="metric-value">${adminUsers}</div><div class="metric-foot">Proteção mínima para gestão</div></div>`,
        `<div class="card metric-card warning-card"><div class="metric-label">Usuários inativos</div><div class="metric-value">${inactiveUsers}</div><div class="metric-foot">Revise acessos antigos quando necessário</div></div>`,
        `<div class="card metric-card"><div class="metric-label">Backup e restore</div><div class="metric-value">OK</div><div class="metric-foot">Fluxo com confirmação reforçada</div></div>`,
      ]
    : [
        `<div class="card metric-card"><div class="metric-label">Meu perfil</div><div class="metric-value">${escapeHtml(user.name || '-')}</div><div class="metric-foot">Atualize login e dados sempre que necessário</div></div>`,
        `<div class="card metric-card"><div class="metric-label">Meu acesso</div><div class="metric-value">${roleLabel(user.role || 'user')}</div><div class="metric-foot">Funções administrativas ficam ocultas para seu perfil</div></div>`,
      ];
  $('adminStats').innerHTML = cards.join('');
}

function updatePermissionUI(isAdmin) {
  document.querySelectorAll('.admin-only').forEach((element) => {
    element.classList.toggle('hidden', !isAdmin);
  });
}

function renderAdministration() {
  const user = state.user || {};
  const isAdmin = Boolean(user.is_admin || user.role === 'admin');
  $('sessionUserName').textContent = user.name || 'Aguardando login';
  $('sessionUserMeta').textContent = user.email ? `${user.email} · ${roleLabel(user.role)}` : 'Entre para carregar o seu perfil';
  updatePermissionUI(isAdmin);
  $('profileName').value = user.name || '';
  $('profileEmail').value = user.email || '';
  $('profileRoleBadge').innerHTML = `<span class="badge ${isAdmin ? 'admin' : 'user'}">${roleLabel(user.role || 'user')}</span>`;
  $('profileSecurityHint').textContent = isAdmin
    ? 'Você pode alterar seu login, trocar a senha e também administrar usuários, backups e ações críticas.'
    : 'Você pode alterar seu login e trocar sua senha. A gestão de usuários, backups e ações críticas fica oculta para o seu perfil.';
  renderAdminStats();
  renderUsers();
}

function buildMonthlyReportRows() {
  return Array.isArray(state.report?.monthly_rows) ? state.report.monthly_rows : [];
}

function renderReport() {
  const totals = state.report.totals || {};
  const month = state.report.month || $('reportMonth').value || currentMonth;
  const monthLabel = state.report.month_label || month || '-';
  const monthlyRows = buildMonthlyReportRows();
  updateReportFilterOptions();
  if (state.report?.filters) {
    $('reportAccountFilter').value = state.report.filters.account_id ? String(state.report.filters.account_id) : '';
    $('reportApplicationFilter').value = state.report.filters.application_id ? String(state.report.filters.application_id) : '';
    $('reportTypeFilter').value = state.report.filters.app_type || '';
  }
  const totalSaldoInicial = monthlyRows.reduce((sum, item) => sum + Number(item.saldoInicial || 0), 0);
  const totalAporte = monthlyRows.reduce((sum, item) => sum + Number(item.aporte || 0), 0);
  const totalRendimentoReais = monthlyRows.reduce((sum, item) => sum + Number(item.rendimentoReais || 0), 0);
  const totalSaldoFinal = monthlyRows.reduce((sum, item) => sum + Number(item.saldoFinal || 0), 0);
  const totalAcumulado = monthlyRows.reduce((sum, item) => sum + Number(item.totalAcumulado || 0), 0);
  const totalRendimentoPercentual = totalSaldoInicial > 0 ? Number((totalRendimentoReais / totalSaldoInicial * 100).toFixed(4)) : 0;

  $('reportCards').innerHTML = [
    metricCard('Mês', monthLabel, 'Competência selecionada'),
    metricCard('Saldo inicial', formatCurrency(totalSaldoInicial), 'Base do cálculo da rentabilidade'),
    metricCard('Patrimônio', formatCurrency(totals.patrimonio), 'No fechamento do mês filtrado'),
    metricCard('Aportes', formatCurrency(totals.aportes), 'Competência do mês'),
    metricCard('Resgates', formatCurrency(totals.resgates), 'Competência do mês'),
    metricCard('Rendimentos', formatCurrency(totals.rendimentos), 'Competência do mês'),
    metricCard('Rentabilidade', `${formatPercent(totalRendimentoPercentual)}%`, 'Calculada sobre o saldo inicial filtrado'),
    metricCard('Saldo final', formatCurrency(totalSaldoFinal), 'Fechamento projetado do período')
  ].join('');

  $('reportExecutiveSummary').innerHTML = `Em <strong>${escapeHtml(monthLabel)}</strong>, a carteira começou com <strong>${formatCurrency(totalSaldoInicial)}</strong>, recebeu <strong>${formatCurrency(totalAporte)}</strong> em aporte líquido e gerou <strong>${formatCurrency(totalRendimentoReais)}</strong> em rendimentos do mês, equivalente a <strong>${formatPercent(totalRendimentoPercentual)}%</strong>. O saldo final projetado ficou em <strong>${formatCurrency(totalSaldoFinal)}</strong>, com <strong>${formatCurrency(totalAcumulado)}</strong> acumulados em dividendos + rendimentos.`;

  $('reportTypeBars').innerHTML = barList(state.report.portfolio_by_type || [], 'Sem posições para o mês selecionado.');
  $('reportAccountsTable').innerHTML = (state.report.portfolio_by_account || []).length
    ? state.report.portfolio_by_account.map((item) => `
        <tr>
          <td>${escapeHtml(item.account_name)}</td>
          <td>${formatCurrency(item.value)}</td>
        </tr>
      `).join('')
    : '<tr><td colspan="2" class="table-empty">Nenhum valor consolidado para o mês.</td></tr>';

  $('reportMonthlyTable').innerHTML = monthlyRows.length
    ? `${monthlyRows.map((item) => `
        <tr>
          <td>${escapeHtml(item.institution)}</td>
          <td>${escapeHtml(item.application_name)}</td>
          <td>${formatCurrency(item.saldoInicial)}</td>
          <td>${formatCurrency(item.aporte)}</td>
          <td>${formatCurrency(item.rendimentoReais)}</td>
          <td>${formatPercent(item.rendimentoPercentual)}%</td>
          <td>${formatCurrency(item.saldoFinal)}</td>
          <td>${formatCurrency(item.totalAcumulado)}</td>
        </tr>
      `).join('')}
      <tr>
        <td colspan="2"><strong>Total da carteira</strong></td>
        <td><strong>${formatCurrency(totalSaldoInicial)}</strong></td>
        <td><strong>${formatCurrency(totalAporte)}</strong></td>
        <td><strong>${formatCurrency(totalRendimentoReais)}</strong></td>
        <td><strong>${formatPercent(totalRendimentoPercentual)}%</strong></td>
        <td><strong>${formatCurrency(totalSaldoFinal)}</strong></td>
        <td><strong>${formatCurrency(totalAcumulado)}</strong></td>
      </tr>`
    : '<tr><td colspan="8" class="table-empty">Cadastre aplicações e lançamentos para gerar a tabela mensal consolidada.</td></tr>';
}

function clearAccountForm() {
  $('accountId').value = '';
  $('accountName').value = '';
  $('accountInstitution').value = '';
  $('accountCurrency').value = 'BRL';
  $('accountNotes').value = '';
}

function clearApplicationForm() {
  $('applicationId').value = '';
  $('applicationName').value = '';
  $('applicationCode').value = '';
  $('applicationInitialValue').value = '0';
  $('applicationNotes').value = '';
  $('applicationType').value = 'Renda Fixa';
}

function clearMovementForm() {
  $('movementId').value = '';
  $('movementKind').value = 'aporte';
  $('movementDate').value = today;
  $('movementCompetence').value = currentMonth;
  $('movementAmount').value = '';
  $('movementNotes').value = '';
}

function clearDividendForm() {
  $('dividendId').value = '';
  $('dividendDate').value = today;
  $('dividendCompetence').value = currentMonth;
  $('dividendGross').value = '';
  $('dividendNet').value = '';
  $('dividendNotes').value = '';
}

function clearEarningForm() {
  $('earningId').value = '';
  $('earningDate').value = today;
  $('earningCompetence').value = currentMonth;
  $('earningPreviousBalance').value = '';
  $('earningCurrentBalance').value = '';
  $('earningAmount').value = '';
  $('earningPercent').value = '';
  $('earningNotes').value = '';
  recalculateEarningFields();
}

function clearUserForm() {
  if (!$('userId')) return;
  $('userId').value = '';
  $('userName').value = '';
  $('userEmail').value = '';
  $('userRole').value = 'user';
  $('userActive').value = '1';
  $('userPassword').value = '';
  $('userPasswordConfirm').value = '';
  $('userPasswordLabel').textContent = 'Senha inicial';
  $('userSubmitLabel').textContent = 'Salvar usuário';
}

function findById(collection, id) {
  return collection.find((item) => String(item.id) === String(id));
}

function editAccount(id) {
  const item = findById(state.accounts, id);
  if (!item) return;
  setActiveSection('contas');
  $('accountId').value = item.id;
  $('accountName').value = item.name || '';
  $('accountInstitution').value = item.institution || '';
  $('accountCurrency').value = item.currency || 'BRL';
  $('accountNotes').value = item.notes || '';
}

function editApplication(id) {
  const item = findById(state.applications, id);
  if (!item) return;
  setActiveSection('aplicacoes');
  $('applicationId').value = item.id;
  $('applicationAccount').value = item.account_id;
  $('applicationType').value = item.type || 'Outros';
  $('applicationName').value = item.name || '';
  $('applicationCode').value = item.code || '';
  $('applicationInitialValue').value = item.initial_value || 0;
  $('applicationNotes').value = item.notes || '';
}

function editMovement(id) {
  const item = findById(state.movements, id);
  if (!item) return;
  setActiveSection('movimentos');
  $('movementId').value = item.id;
  $('movementKind').value = item.kind;
  $('movementApplication').value = item.application_id;
  $('movementDate').value = item.date || today;
  $('movementCompetence').value = (item.competence || currentMonth).slice(0, 7);
  $('movementAmount').value = item.amount || '';
  $('movementNotes').value = item.notes || '';
}

function editDividend(id) {
  const item = findById(state.dividends, id);
  if (!item) return;
  setActiveSection('dividendos');
  $('dividendId').value = item.id;
  $('dividendApplication').value = item.application_id;
  $('dividendDate').value = item.payment_date || today;
  $('dividendCompetence').value = (item.competence || currentMonth).slice(0, 7);
  $('dividendGross').value = item.gross_amount || '';
  $('dividendNet').value = item.net_amount || '';
  $('dividendNotes').value = item.notes || '';
}

function editEarning(id) {
  const item = findById(state.earnings, id);
  if (!item) return;
  setActiveSection('rendimentos');
  $('earningId').value = item.id;
  $('earningApplication').value = item.application_id;
  $('earningDate').value = item.payment_date || today;
  $('earningCompetence').value = (item.competence || currentMonth).slice(0, 7);
  const previousBalance = Number(item.previous_balance || (Number(item.current_balance || 0) - Number(item.amount || 0)) || 0);
  $('earningPreviousBalance').value = previousBalance ? previousBalance.toFixed(2) : '';
  $('earningCurrentBalance').value = item.current_balance || '';
  $('earningAmount').value = item.amount || '';
  $('earningPercent').value = item.percent || '';
  $('earningNotes').value = item.notes || '';
  recalculateEarningFields();
}

function editUser(id) {
  const item = findById(state.users, id);
  if (!item) return;
  setActiveSection('administracao');
  $('userId').value = item.id;
  $('userName').value = item.name || '';
  $('userEmail').value = item.email || '';
  $('userRole').value = item.role || 'user';
  $('userActive').value = item.active ? '1' : '0';
  $('userPassword').value = '';
  $('userPasswordConfirm').value = '';
  $('userPasswordLabel').textContent = 'Nova senha (opcional)';
  $('userSubmitLabel').textContent = 'Salvar alterações';
}

async function toggleUserStatus(id) {
  const item = findById(state.users, id);
  if (!item) return;
  try {
    const data = await api(`/api/users/${id}`, {
      method: 'PUT',
      body: JSON.stringify({
        name: item.name,
        email: item.email,
        role: item.role,
        active: !item.active,
      }),
    });
    showFlash(data.message || 'Status do usuário atualizado.', 'success');
    await loadBootstrap();
    setActiveSection('administracao');
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function resetUserPasswordPrompt(id) {
  const item = findById(state.users, id);
  if (!item) return;
  const password = window.prompt(`Digite a nova senha para ${item.name}:`);
  if (!password) return;
  const confirmPassword = window.prompt(`Confirme a nova senha para ${item.name}:`);
  if (password !== confirmPassword) {
    showFlash('As senhas digitadas não conferem.', 'error');
    return;
  }
  try {
    const data = await api(`/api/users/${id}/password`, {
      method: 'POST',
      body: JSON.stringify({ new_password: password }),
    });
    showFlash(data.message || 'Senha redefinida com sucesso.', 'success');
  } catch (error) {
    showFlash(error.message, 'error');
  }
}

async function deleteEntity(url, label) {
  if (!confirm(`Confirma excluir ${label}?`)) return;
  const data = await api(url, { method: 'DELETE' });
  showFlash(data.message || 'Registro excluído.', 'success');
  await loadBootstrap();
}

const deleteAccount = (id) => deleteEntity(`/api/accounts/${id}`, 'esta conta e tudo que depende dela');
const deleteApplication = (id) => deleteEntity(`/api/applications/${id}`, 'esta aplicação e seus lançamentos');
const deleteMovement = (id) => deleteEntity(`/api/movements/${id}`, 'este lançamento');
const deleteDividend = (id) => deleteEntity(`/api/dividends/${id}`, 'este dividendo');
const deleteEarning = (id) => deleteEntity(`/api/earnings/${id}`, 'este rendimento');

async function loadBootstrap(month = $('reportMonth').value || currentMonth) {
  const data = await api(`/api/bootstrap?month=${month}`);
  state.user = data.user;
  state.users = data.users || [];
  state.dashboard = data.dashboard;
  state.accounts = data.accounts;
  state.applications = data.applications;
  state.movements = data.movements;
  state.dividends = data.dividends;
  state.earnings = data.earnings;
  state.snapshots = data.snapshots;
  state.imports = data.imports;
  state.report = data.report;
  $('loginOverlay').classList.add('hidden');
  updateSelects();
  renderDashboard();
  renderAccounts();
  renderApplications();
  renderMovements();
  renderDividends();
  renderEarnings();
  renderImportSection();
  renderAdministration();
  renderReport();
}

async function saveJsonForm(idField, urlBase, payload) {
  const editId = $(idField).value;
  const url = editId ? `${urlBase}/${editId}` : urlBase;
  const method = editId ? 'PUT' : 'POST';
  const data = await api(url, { method, body: JSON.stringify(payload) });
  showFlash(data.message || 'Registro salvo.', 'success');
  await loadBootstrap();
}

function bindForms() {
  $('accountForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await saveJsonForm('accountId', '/api/accounts', {
        name: $('accountName').value,
        institution: $('accountInstitution').value,
        currency: $('accountCurrency').value,
        notes: $('accountNotes').value,
      });
      clearAccountForm();
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('applicationForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await saveJsonForm('applicationId', '/api/applications', {
        account_id: $('applicationAccount').value,
        type: $('applicationType').value,
        name: $('applicationName').value,
        code: $('applicationCode').value,
        initial_value: $('applicationInitialValue').value,
        notes: $('applicationNotes').value,
      });
      clearApplicationForm();
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('movementForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await saveJsonForm('movementId', '/api/movements', {
        application_id: $('movementApplication').value,
        kind: $('movementKind').value,
        date: $('movementDate').value,
        competence: $('movementCompetence').value,
        amount: $('movementAmount').value,
        notes: $('movementNotes').value,
      });
      clearMovementForm();
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('dividendForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await saveJsonForm('dividendId', '/api/dividends', {
        application_id: $('dividendApplication').value,
        payment_date: $('dividendDate').value,
        competence: $('dividendCompetence').value,
        gross_amount: $('dividendGross').value,
        net_amount: $('dividendNet').value,
        notes: $('dividendNotes').value,
      });
      clearDividendForm();
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('earningForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const previousBalance = Number($('earningPreviousBalance').value || 0);
      const currentBalance = Number($('earningCurrentBalance').value || 0);
      const amount = Number($('earningAmount').value || 0);
      const percent = Number($('earningPercent').value || 0);
      if (previousBalance <= 0 || currentBalance <= 0 || amount <= 0) {
        showFlash('Informe saldo anterior e saldo atual para calcular um rendimento positivo.', 'error');
        return;
      }
      await saveJsonForm('earningId', '/api/earnings', {
        application_id: $('earningApplication').value,
        payment_date: $('earningDate').value,
        competence: $('earningCompetence').value,
        previous_balance: previousBalance,
        current_balance: currentBalance,
        amount,
        percent,
        notes: $('earningNotes').value,
      });
      clearEarningForm();
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const data = await api('/api/login', {
        method: 'POST',
        body: JSON.stringify({ email: $('loginEmail').value, password: $('loginPassword').value })
      });
      showFlash(data.message || 'Login realizado.', 'success');
      await loadBootstrap();
      $('loginPassword').value = '';
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('importForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = $('importFile').files[0];
    if (!file) {
      showFlash('Selecione um arquivo .xlsx para importar.', 'error');
      return;
    }
    try {
      const form = new FormData();
      form.append('file', file);
      const data = await api('/api/import/xlsx', { method: 'POST', body: form });
      renderImportSummary(data.summary, data.message);
      await loadBootstrap();
      showFlash(data.message || 'Planilha importada com sucesso.', 'success');
      $('importFile').value = '';
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('profileForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const data = await api('/api/profile', {
        method: 'PUT',
        body: JSON.stringify({
          name: $('profileName').value,
          email: $('profileEmail').value,
        }),
      });
      showFlash(data.message || 'Seus dados foram atualizados.', 'success');
      await loadBootstrap();
      setActiveSection('administracao');
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('passwordForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const newPassword = $('profileNewPassword').value;
    const confirmPassword = $('profileConfirmPassword').value;
    if (newPassword !== confirmPassword) {
      showFlash('A confirmação da nova senha não confere.', 'error');
      return;
    }
    try {
      const data = await api('/api/profile/password', {
        method: 'POST',
        body: JSON.stringify({
          current_password: $('profileCurrentPassword').value,
          new_password: newPassword,
        }),
      });
      showFlash(data.message || 'Senha atualizada.', 'success');
      $('profileCurrentPassword').value = '';
      $('profileNewPassword').value = '';
      $('profileConfirmPassword').value = '';
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('userForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const editId = $('userId').value;
    const password = $('userPassword').value.trim();
    const confirmPassword = $('userPasswordConfirm').value.trim();
    if (!editId && !password) {
      showFlash('Informe uma senha inicial para o novo usuário.', 'error');
      return;
    }
    if ((password || confirmPassword) && password !== confirmPassword) {
      showFlash('As senhas informadas para o usuário não conferem.', 'error');
      return;
    }
    try {
      const payload = {
        name: $('userName').value,
        email: $('userEmail').value,
        role: $('userRole').value,
        active: $('userActive').value === '1',
      };
      let data;
      if (editId) {
        data = await api(`/api/users/${editId}`, { method: 'PUT', body: JSON.stringify(payload) });
        if (password) {
          await api(`/api/users/${editId}/password`, { method: 'POST', body: JSON.stringify({ new_password: password }) });
        }
      } else {
        data = await api('/api/users', {
          method: 'POST',
          body: JSON.stringify({ ...payload, password }),
        });
      }
      showFlash(data.message || 'Usuário salvo com sucesso.', 'success');
      clearUserForm();
      await loadBootstrap();
      setActiveSection('administracao');
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('restoreBackupForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = $('restoreBackupFile').files[0];
    if (!file) {
      showFlash('Selecione um arquivo de backup .zip ou .db.', 'error');
      return;
    }
    openRestoreConfirmation(file);
  });

  $('restoreConfirmForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (($('restoreConfirmText').value || '').trim().toUpperCase() !== 'RESTAURAR') {
      showFlash('Digite RESTAURAR para confirmar a restauração.', 'error');
      return;
    }
    if (!pendingRestoreFile) {
      showFlash('Nenhum arquivo de restauração pendente.', 'error');
      closeRestoreConfirmation();
      return;
    }
    try {
      await restoreBackupNow(pendingRestoreFile);
      closeRestoreConfirmation();
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });
}


function renderImportSummary(summary, message) {
  if (!summary) return;
  $('importSummary').className = 'summary-box';
  $('importSummary').innerHTML = `
    <strong>${escapeHtml(message || 'Importação concluída')}</strong><br>
    Contas criadas: ${summary.accounts_created || 0} · Aplicações criadas: ${summary.applications_created || 0}<br>
    Snapshots importados: ${summary.snapshots_imported || 0} · Rendimentos importados: ${summary.earnings_imported || 0}
  `;
}

function openRestoreConfirmation(file) {
  pendingRestoreFile = file;
  $('restoreConfirmFileName').textContent = file?.name || '-';
  $('restoreConfirmText').value = '';
  $('restoreConfirmOverlay').classList.remove('hidden');
  $('restoreConfirmText').focus();
}

function closeRestoreConfirmation() {
  pendingRestoreFile = null;
  $('restoreConfirmText').value = '';
  $('restoreConfirmOverlay').classList.add('hidden');
}

async function restoreBackupNow(file) {
  const form = new FormData();
  form.append('file', file);
  const data = await api('/api/backup/restore', { method: 'POST', body: form });
  $('backupStatus').className = 'summary-box';
  $('backupStatus').textContent = data.message || 'Backup restaurado.';
  $('restoreBackupFile').value = '';
  $('sessionUserName').textContent = 'Backup restaurado';
  $('sessionUserMeta').textContent = 'Faça login novamente para continuar';
  $('loginOverlay').classList.remove('hidden');
  showFlash(data.message || 'Backup restaurado com sucesso.', 'info');
}

function bindButtons() {
  document.querySelectorAll('#menuNav button').forEach((button) => {
    button.addEventListener('click', () => setActiveSection(button.dataset.section));
  });

  $('btnRefreshDashboard').addEventListener('click', () => loadBootstrap().catch((error) => showFlash(error.message, 'error')));
  $('btnLoadReport').addEventListener('click', async () => {
    try {
      const data = await api(`/api/reports/monthly?${buildReportQueryString()}`);
      state.report = data.report;
      renderReport();
      showFlash('Relatório mensal atualizado.', 'success');
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('btnExportReport').addEventListener('click', () => {
    window.location.href = `/api/reports/monthly/export?${buildReportQueryString()}`;
  });

  $('btnImportSample').addEventListener('click', async () => {
    try {
      const data = await api('/api/import/sample', { method: 'POST' });
      renderImportSummary(data.summary, data.message);
      await loadBootstrap();
      showFlash(data.message || 'Planilha exemplo importada.', 'success');
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('btnResetLaunches').addEventListener('click', async () => {
    if (!confirm('Confirma zerar todos os lançamentos e snapshots, mantendo apenas os cadastros?')) return;
    try {
      const data = await api('/api/actions/reset-launches', { method: 'POST' });
      await loadBootstrap();
      showFlash(data.message || 'Lançamentos zerados.', 'success');
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('btnResetAll').addEventListener('click', async () => {
    if (!confirm('Confirma apagar toda a base, incluindo contas e aplicações?')) return;
    try {
      const data = await api('/api/actions/reset-all', { method: 'POST' });
      clearAccountForm();
      clearApplicationForm();
      clearMovementForm();
      clearDividendForm();
      clearEarningForm();
      await loadBootstrap();
      showFlash(data.message || 'Base apagada.', 'success');
    } catch (error) {
      showFlash(error.message, 'error');
    }
  });

  $('btnDownloadBackup').addEventListener('click', () => {
    $('backupStatus').className = 'summary-box';
    $('backupStatus').textContent = 'Download do backup iniciado.';
    window.location.href = '/api/backup/download';
  });

  $('btnCancelRestoreConfirm').addEventListener('click', () => closeRestoreConfirmation());
  $('restoreConfirmOverlay').addEventListener('click', (event) => {
    if (event.target === $('restoreConfirmOverlay')) closeRestoreConfirmation();
  });

  $('btnLogout').addEventListener('click', async () => {
    try {
      await api('/api/logout', { method: 'POST' });
    } catch (error) {
      // ignore
    }
    $('loginOverlay').classList.remove('hidden');
    $('sessionUserName').textContent = 'Sessão encerrada';
    $('sessionUserMeta').textContent = 'Faça login novamente para continuar';
    showFlash('Sessão encerrada.', 'info');
  });
}

function setDefaults() {
  $('movementDate').value = today;
  $('movementCompetence').value = currentMonth;
  $('dividendDate').value = today;
  $('dividendCompetence').value = currentMonth;
  $('earningDate').value = today;
  $('earningCompetence').value = currentMonth;
  $('reportMonth').value = currentMonth;
  fillStaticTypes();
  clearUserForm();
  recalculateEarningFields();
}

async function init() {
  setDefaults();
  bindForms();
  bindButtons();
  $('earningApplication').addEventListener('change', () => recalculateEarningFields());
  $('earningCompetence').addEventListener('change', () => recalculateEarningFields());
  $('reportAccountFilter').addEventListener('change', () => updateReportFilterOptions());
  try {
    await loadBootstrap();
  } catch (error) {
    $('loginOverlay').classList.remove('hidden');
  }
}

window.clearAccountForm = clearAccountForm;
window.clearApplicationForm = clearApplicationForm;
window.clearMovementForm = clearMovementForm;
window.clearDividendForm = clearDividendForm;
window.clearEarningForm = clearEarningForm;
window.clearUserForm = clearUserForm;
window.recalculateEarningFields = recalculateEarningFields;
window.useProjectedBalanceForEarning = useProjectedBalanceForEarning;
window.editAccount = editAccount;
window.editApplication = editApplication;
window.editMovement = editMovement;
window.editDividend = editDividend;
window.editEarning = editEarning;
window.editUser = editUser;
window.toggleUserStatus = toggleUserStatus;
window.resetUserPasswordPrompt = resetUserPasswordPrompt;
window.closeRestoreConfirmation = closeRestoreConfirmation;
window.deleteAccount = (id) => deleteAccount(id).catch((error) => showFlash(error.message, 'error'));
window.deleteApplication = (id) => deleteApplication(id).catch((error) => showFlash(error.message, 'error'));
window.deleteMovement = (id) => deleteMovement(id).catch((error) => showFlash(error.message, 'error'));
window.deleteDividend = (id) => deleteDividend(id).catch((error) => showFlash(error.message, 'error'));
window.deleteEarning = (id) => deleteEarning(id).catch((error) => showFlash(error.message, 'error'));

document.addEventListener('DOMContentLoaded', init);
