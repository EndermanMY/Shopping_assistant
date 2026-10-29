import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QRCodeSVG } from 'qrcode.react';
import {
  ArrowDownLeft, ArrowRight, ArrowUpRight, Bell, Check, CheckCircle2, ChevronDown,
  ChevronRight, CircleHelp, Clock3, Copy, ExternalLink, Link2, ListTodo,
  Loader2, LogOut, Menu, Monitor, MoreHorizontal, Pause, Play, Plus,
  Radio, RefreshCw, Settings2, ShieldCheck, ShoppingBag, Smartphone, Terminal,
  Trash2, Volume2, VolumeX, Wifi, X, Zap,
} from 'lucide-react';
import './style.css';

type Product = { platform: 'jd'; sku: string; title: string; spec: string; url: string;
  reservation_start: string | null; reservation_end: string | null;
  sale_start: string | null; sale_end: string | null; reservation_required: boolean; checked_at: string };
type Config = { product: Product; quantity: number; scheduled_at: string; max_total_cents: number; address_hint: string; window_seconds: number; advance_ms?: number; submit_mode?: 'dom' | 'api' };
type Order = { outcome: 'created' | 'unknown' | 'failed'; order_id?: string; total_cents?: number; message: string; url?: string; payment_entered?: boolean; payment_flow?: 'not_attempted' | 'entered' | 'entered_and_exited' | 'not_confirmed' };
type Task = { id: string; config: Config; status: string; message: string; created_at: string; updated_at: string; result: Order | null; submit_intent: string | null; submission_order_id?: string | null; screenshot: string | null };
type Log = { id: number; task_id: string | null; time: string; level: string; message: string };
type Account = { browser_open: boolean; logged_in: boolean; message: string; access_paused?: boolean; browser_network?: string };
type Overview = { tasks: Task[]; events: Log[]; account: Account; lan_urls: string[]; busy_task: string | null; server_time: string };

const active = new Set(['preparing', 'reserving', 'preparing_cart', 'preparing_checkout', 'scheduled', 'running', 'submitting', 'entering_payment', 'locking', 'verifying']);
const labels: Record<string, string> = { draft: '待启用', preparing: '检查中', reserving: '确认预约', preparing_cart: '加入购物车', preparing_checkout: '提前核对结算', scheduled: '等待开抢', running: '抢购中', waiting_human: '需要处理', submitting: '提交订单', entering_payment: '进入付款', locking: '锁单中', verifying: '核对订单号', succeeded: '抢购成功', failed: '本场结束', stopped: '已停止', interrupted: '待检查' };
const stamp = (date: string, full = false) => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', ...(full ? { month: '2-digit', day: '2-digit' } as const : {}), hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(date));
const executionStart = (config: Config) => new Date(new Date(config.scheduled_at).getTime() - (config.advance_ms || 0)).toISOString();
const executionEnd = (config: Config) => new Date(new Date(executionStart(config)).getTime() + config.window_seconds * 1000).toISOString();
const exactStamp = (date: string) => `${stamp(date, true)}.${String(new Date(date).getUTCMilliseconds()).padStart(3, '0')}`;
const expired = (task: Task, now: number) => !task.submit_intent && !active.has(task.status) && now > new Date(executionEnd(task.config)).getTime();
const amount = (cents: number) => (cents / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const chinaInput = (iso: string) => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(iso)).replace(' ', 'T');

async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, { method, credentials: 'same-origin', headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('session-expired'));
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请检查填写的信息后重试');
  return data as T;
}

function Countdown({ target, now }: { target: string; now: number }) {
  const delta = Math.max(0, Math.floor((new Date(target).getTime() - now) / 1000));
  const hours = Math.floor(delta / 3600), minutes = Math.floor(delta % 3600 / 60), seconds = delta % 60;
  return <span className="countdown">{[hours, minutes, seconds].map((n, i) => <React.Fragment key={i}>{i > 0 && <b>:</b>}<span>{String(n).padStart(2, '0')}</span></React.Fragment>)}</span>;
}

function App() {
  const [authenticated, setAuthenticated] = useState(false);
  const [booting, setBooting] = useState(true);
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [overview, setOverview] = useState<Overview | null>(null);
  const [connected, setConnected] = useState(false);
  const [clock, setClock] = useState(Date.now());
  const offset = useRef(0);
  const [tab, setTab] = useState('all');
  const [search, setSearch] = useState('');
  const [modal, setModal] = useState<'create' | 'connect' | 'help' | 'deleted' | null>(null);
  const [deletedTasks, setDeletedTasks] = useState<Task[]>([]);
  const [resetTarget, setResetTarget] = useState<Task | null>(null);
  const [resetConfirmed, setResetConfirmed] = useState(false);
  const [resetError, setResetError] = useState('');
  const [editing, setEditing] = useState<Task | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [pending, setPending] = useState('');
  const [sound, setSound] = useState(false);
  const audio = useRef<AudioContext | null>(null);
  const notified = useRef(new Set<string>());
  const [navOpen, setNavOpen] = useState(false);

  const load = async () => {
    try {
      const data = await api<Overview>('/overview');
      offset.current = new Date(data.server_time).getTime() - Date.now(); setClock(new Date(data.server_time).getTime());
      setOverview(data); setAuthenticated(true); setError('');
    } catch { /* 401 transitions to access form; transient disconnect preserves data. */ }
    finally { setBooting(false); }
  };
  useEffect(() => {
    const executionStart = (config: Config) => new Date(new Date(config.scheduled_at).getTime() - (config.advance_ms || 0)).toISOString();
const executionEnd = (config: Config) => new Date(new Date(executionStart(config)).getTime() + config.window_seconds * 1000).toISOString();
const exactStamp = (date: string) => `${stamp(date, true)}.${String(new Date(date).getUTCMilliseconds()).padStart(3, '0')}`;
const expired = () => { setAuthenticated(false); setOverview(null); };
    window.addEventListener('session-expired', expired);
    void load();
    const ticker = window.setInterval(() => setClock(Date.now() + offset.current), 1000);
    return () => { window.removeEventListener('session-expired', expired); clearInterval(ticker); };
  }, []);
  useEffect(() => {
    if (!authenticated) return;
    let socket: WebSocket | null = null, retry: ReturnType<typeof setTimeout>;
    let disposed = false;
    const connect = () => {
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
      socket.onopen = () => { setConnected(true); void load(); };
      socket.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.type === 'task' && message.task.status === 'succeeded') setNotice(message.task.message);
        setOverview(previous => {
          if (!previous) return previous;
          if (message.type === 'task') {
            const rest = previous.tasks.filter(t => t.id !== message.task.id);
            return { ...previous, tasks: [message.task, ...rest].sort((a, b) => b.created_at.localeCompare(a.created_at)), busy_task: active.has(message.task.status) ? message.task.id : previous.busy_task === message.task.id ? null : previous.busy_task };
          }
          if (message.type === 'deleted') return { ...previous, tasks: previous.tasks.filter(t => t.id !== message.task_id), events: previous.events.filter(e => e.task_id !== message.task_id), busy_task: previous.busy_task === message.task_id ? null : previous.busy_task };
          if (message.type === 'log') return { ...previous, events: [...previous.events, message.event].slice(-300) };
          return previous;
        });
      };
      socket.onclose = () => { setConnected(false); if (!disposed) retry = setTimeout(connect, 3000); };
    };
    connect();
    const status = setInterval(() => { void api<Account>('/account/status').then(account => setOverview(o => o ? { ...o, account } : o)).catch(() => {}); }, 12000);
    return () => { disposed = true; clearTimeout(retry); clearInterval(status); socket?.close(); };
  }, [authenticated]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(''), 6000);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    for (const task of overview?.tasks || []) {
      if ((task.status === 'succeeded' || task.status === 'waiting_human') && !notified.current.has(task.id + task.updated_at)) {
        notified.current.add(task.id + task.updated_at);
        if (sound && audio.current) {
          const oscillator = audio.current.createOscillator(), gain = audio.current.createGain();
          oscillator.connect(gain); gain.connect(audio.current.destination);
          oscillator.frequency.value = task.status === 'succeeded' ? 880 : 520;
          gain.gain.setValueAtTime(0.08, audio.current.currentTime);
          gain.gain.exponentialRampToValueAtTime(0.001, audio.current.currentTime + 0.65);
          oscillator.start(); oscillator.stop(audio.current.currentTime + 0.7);
        }
      }
    }
  }, [overview?.tasks, sound]);

  const action = async (id: string, type: string) => {
    setPending(id);
    try { await api(`/tasks/${id}/${type}`, 'POST'); await load(); }
    catch (e) { setNotice((e as Error).message); }
    finally { setPending(''); }
  };
  const loginBrowser = async () => {
    setPending('login');
    try { const data = await api<{ message: string }>('/account/login', 'POST'); setNotice(data.message); }
    catch (e) { setNotice((e as Error).message); }
    finally { setPending(''); }
  };
  const remove = async (task: Task) => {
    setPending(task.id);
    try { await api(`/tasks/${task.id}`, 'DELETE'); setDetailId(null); await load(); setNotice('任务已删除，可在已删除任务中恢复；京东订单不受影响。'); }
    catch (e) { setNotice((e as Error).message); }
    finally { setPending(''); }
  };
  const openDeleted = async () => {
    try { setDeletedTasks(await api<Task[]>('/tasks?deleted=true')); setModal('deleted'); }
    catch (e) { setNotice((e as Error).message); }
  };
  const toggleSound = async () => {
    if (!sound) { audio.current ||= new AudioContext(); await audio.current.resume(); }
    setSound(!sound);
  };

  if (booting) return <div className="access-screen"><Loader2 className="spin" size={26} /></div>;
  if (!authenticated) return <div className="access-screen">
    <div className="access-brand"><ShoppingBag size={26} /><span>购物小助手</span></div>
    <form className="access-card" onSubmit={async e => { e.preventDefault(); setError(''); setPending('auth'); try { await api('/auth/login', 'POST', { code }); await load(); setCode(''); } catch (e) { setError((e as Error).message); } finally { setPending(''); } }}>
      <div className="access-icon"><ShieldCheck size={30} /></div><span className="eyebrow">YOUR LOCAL SHOPPING WORKSPACE</span>
      <h1>欢迎回到工作台</h1><p>输入这台电脑上的访问码，连接你的购物助手。</p>
      <label htmlFor="access-code">控制台访问码</label><input id="access-code" autoFocus type="password" autoComplete="current-password" value={code} onChange={e => setCode(e.target.value)} placeholder="在启动终端中查看访问码" required />
      {error && <div className="inline-error" role="alert">{error}</div>}
      <button className="button primary wide" disabled={!!pending}>{pending ? <Loader2 size={16} className="spin" /> : <ArrowRight size={16} />}连接工作台</button>
      <div className="access-foot"><Monitor size={14} /> 数据与京东登录会话保存在你的电脑上</div>
    </form><span className="access-bottom">本地运行 · 京东优先 · 付款由你完成</span>
  </div>;

  const tasks = overview?.tasks || [];
  const running = tasks.filter(t => active.has(t.status));
  const waiting = tasks.filter(t => ['waiting_human', 'interrupted'].includes(t.status));
  const succeeded = tasks.filter(t => t.status === 'succeeded');
  const filtered = tasks.filter(t => (tab === 'all' || tab === 'active' && active.has(t.status) || tab === 'attention' && ['waiting_human', 'interrupted'].includes(t.status) || tab === 'done' && t.status === 'succeeded') && (!search || `${t.config.product.title}${t.config.product.sku}`.includes(search)));
  const nextTask = [...tasks].filter(t => !['succeeded', 'failed', 'stopped'].includes(t.status) && new Date(executionStart(t.config)).getTime() > clock).sort((a, b) => a.config.scheduled_at.localeCompare(b.config.scheduled_at))[0];
  const selected = tasks.find(t => t.id === detailId);
  const account = overview?.account;
  const eventList = detailId ? overview?.events.filter(e => e.task_id === detailId) || [] : overview?.events || [];

  return <div className="app-shell">
    <aside className={`sidebar ${navOpen ? 'open' : ''}`}>
      <a className="brand" href="#" onClick={e => { e.preventDefault(); setTab('all'); setNavOpen(false); }}><span className="brand-icon"><ShoppingBag size={21} /></span><span>购物小助手<small>SHOPPING ASSISTANT</small></span></a>
      <div className="workspace-label">个人工作空间 <span>V1.0</span></div>
      <nav aria-label="工作台导航"><button className="nav-item selected" onClick={() => { setTab('all'); setNavOpen(false); }}><ListTodo size={18} />抢购工作台<ChevronRight size={15} /></button><button className="nav-item" onClick={() => { setModal('connect'); setNavOpen(false); }}><Smartphone size={18} />手机连接</button><button className="nav-item" onClick={() => { setModal('help'); setNavOpen(false); }}><CircleHelp size={18} />使用指南</button></nav>
      <div className="platform-title">购物平台</div><div className="platform-row"><span className="jd-icon">JD</span><span>京东</span><span className="tiny-status">已适配</span></div>
      <div className="platform-hint"><Plus size={15} /><span>更多平台，逐步接入</span></div>
      <div className="sidebar-bottom"><div className="local-badge"><span className="status-dot green" /><div>本地服务运行中<small>电脑负责执行，手机随时查看</small></div></div><button className="quiet-button" onClick={async () => { await api('/auth/logout', 'POST'); setAuthenticated(false); }}><LogOut size={15} />退出控制台</button></div>
    </aside>
    {navOpen && <button className="nav-scrim" aria-label="关闭导航" onClick={() => setNavOpen(false)} />}
    <div className="main-shell">
      <header className="topbar"><div className="breadcrumb"><button className="mobile-menu icon-button" aria-label="打开导航" onClick={() => setNavOpen(!navOpen)}><Menu size={19} /></button><span>工作空间</span><ChevronRight size={14} /><strong>抢购工作台</strong></div><div className="topbar-actions"><span className={`connection ${connected ? '' : 'offline'}`}><span className="status-dot" />{connected ? '实时连接' : '正在重连'}</span><span className="top-divider" /><button className={`icon-button ${sound ? 'sound-on' : ''}`} aria-label={sound ? '关闭声音提醒' : '开启声音提醒'} title={sound ? '声音提醒已开启' : '开启声音提醒'} onClick={() => void toggleSound()}>{sound ? <Volume2 size={18} /> : <VolumeX size={18} />}</button><span className="avatar">我</span></div></header>
      <main>
        <section className="page-heading"><div><div className="eyebrow">READY WHEN IT MATTERS</div><h1>抢购工作台<span className="heading-dot">.</span></h1><p>预约、守候、下单，每一步都心中有数。</p></div><button className="button primary" onClick={() => { setEditing(null); setModal('create'); }}><Plus size={17} />创建抢购任务</button></section>
        <section className="overview-grid" aria-label="任务概览">
          {[{ label: '正在进行', number: running.length, icon: <Zap size={19} />, note: '等待开售与执行中的任务', color: 'violet' }, { label: '需要你处理', number: waiting.length, icon: <Bell size={19} />, note: '登录、页面或订单待确认', color: 'amber' }, { label: '已抢购成功', number: succeeded.length, icon: <CheckCircle2 size={19} />, note: '已核对订单，记得完成付款', color: 'green' }, { label: '全部任务', number: tasks.length, icon: <ShoppingBag size={19} />, note: '你的心仪好物清单', color: 'gray' }].map(metric => <div className="metric" key={metric.label}><div className="metric-top"><span>{metric.label}</span><span className={`metric-icon ${metric.color}`}>{metric.icon}</span></div><div className="metric-value">{metric.number.toString().padStart(2, '0')}<span>项</span></div><small>{metric.note}</small></div>)}
        </section>
        <div className="content-grid"><div className="content-primary">
          <section className="panel task-panel"><div className="panel-heading"><div><h2>我的抢购任务 <span className="count-pill">{tasks.length}</span></h2><p>预约 → 加购 → 提前核对结算 → 到点提交 → 付款界面 → 锁单并显示订单号。</p></div><div className="panel-controls"><button className="text-button" onClick={() => void openDeleted()}><Trash2 size={13} />已删除任务</button><button className="icon-button" aria-label="刷新任务" onClick={() => void load()}><RefreshCw size={16} /></button></div></div><div className="task-toolbar"><div className="tabs" role="tablist" aria-label="任务筛选">{[['all', '全部'], ['active', '进行中'], ['attention', '待处理'], ['done', '已锁单']].map(([value, name]) => <button key={value} role="tab" aria-selected={tab === value} className={tab === value ? 'active' : ''} onClick={() => setTab(value)}>{name}</button>)}</div><input className="search" aria-label="搜索商品或 SKU" placeholder="搜索商品 / SKU" value={search} onChange={e => setSearch(e.target.value)} /></div>
            {!filtered.length ? <div className="empty-state"><div className="empty-illustration"><span className="empty-orbit" /><ShoppingBag size={34} strokeWidth={1.4} /><span className="empty-plus"><Plus size={13} /></span></div><h3>{tasks.length ? '这里暂时没有匹配的任务' : '下一件心仪好物，从这里开始'}</h3><p>{tasks.length ? '试试切换任务状态或搜索其他关键词。' : '添加京东商品，设置开售时间与预算，剩下的交给助手。'}</p>{!tasks.length && <button className="button secondary" onClick={() => { setEditing(null); setModal('create'); }}><Plus size={16} />添加第一个任务</button>}</div> : <div className="task-list">{filtered.map(task => <article className={`task-row ${task.status === 'succeeded' ? 'success-row' : ''}`} key={task.id}><div className="product-icon"><ShoppingBag size={23} /><span>JD</span></div><button className="task-product" onClick={() => setDetailId(task.id)}><strong>{task.config.product.title}</strong><span>{task.config.product.spec || '规格以当前 SKU 为准'}<i />数量 {task.config.quantity}<i />SKU {task.config.product.sku}<i />{task.config.submit_mode === 'api' ? 'API 实验提交' : '页面提交'}</span><small><Clock3 size={12} />{exactStamp(executionStart(task.config))} 开始 · 提前 {task.config.advance_ms || 0} ms · 持续 {task.config.window_seconds} 秒 · 上限 ¥{amount(task.config.max_total_cents)}</small></button><div className="task-state"><span className={`status-badge ${expired(task, clock) ? "expired" : task.status}`}><span className="status-dot" />{expired(task, clock) ? "场次已过期" : labels[task.status]}</span>{task.status === 'scheduled' ? <Countdown target={executionStart(task.config)} now={clock} /> : <span className="task-message">{expired(task, clock) ? '请修改目标场次后启用' : task.message || '准备就绪后启用任务'}</span>}</div><div className="task-actions">{active.has(task.status) ? <button className="button mini stop-button" disabled={pending === task.id} onClick={() => void action(task.id, 'stop')}><Pause size={13} />停止</button> : expired(task, clock) ? <button className="button mini secondary" onClick={() => { setEditing(task); setModal('create'); }}><Settings2 size={13} />修改场次</button> : task.status !== 'succeeded' && <button className="button mini secondary" disabled={pending === task.id || !!overview?.busy_task} onClick={() => void action(task.id, ['waiting_human', 'interrupted'].includes(task.status) || task.submit_intent ? 'resume' : 'start')}><Play size={13} />{task.submit_intent ? '核验订单' : ['waiting_human', 'interrupted'].includes(task.status) ? '恢复' : '启用'}</button>}{!active.has(task.status) && task.status !== 'succeeded' && !task.submission_order_id && !task.result?.order_id && task.result?.outcome !== 'created' && <button className="button mini secondary" disabled={pending === task.id || !!overview?.busy_task} aria-label={`重置任务 ${task.config.product.sku}`} onClick={() => { setResetTarget(task); setResetConfirmed(false); setResetError(''); }}><RefreshCw size={13} />重置</button>}<button className="icon-button" disabled={pending === task.id} aria-label={`删除任务 ${task.config.product.sku}`} onClick={() => void remove(task)}><Trash2 size={15} /></button><button className="icon-button" aria-label={`查看任务 ${task.config.product.sku}`} onClick={() => setDetailId(task.id)}><MoreHorizontal size={19} /></button></div></article>)}</div>}
            <div className="panel-footer"><ShieldCheck size={14} /><span>提交前核对商品、地址和金额 · 下单成功后由你付款</span></div>
          </section>
          <section className="panel log-panel"><div className="panel-heading"><div className="log-title"><Terminal size={17} /><h2>运行动态</h2><span className="live-tag">LIVE</span></div><span className="muted small">北京时间</span></div><div className="log-list" aria-live="polite">{!eventList.length ? <div className="log-empty"><span className="log-line-dot" /><span>助手已就绪，任务启动后会在这里记录每一步。</span><time>{stamp(new Date(clock).toISOString())}</time></div> : [...eventList].reverse().slice(0, 60).map(log => <div className={`log-entry ${log.level}`} key={log.id}><time>{stamp(log.time)}</time><span className="log-line-dot" /><span>{log.message}</span>{log.task_id && <button aria-label="查看相关任务" onClick={() => setDetailId(log.task_id)}><ArrowUpRight size={13} /></button>}</div>)}</div></section>
        </div><aside className="content-aside">
          <section className="panel account-panel"><div className="aside-heading"><h2>京东账号</h2><span className="jd-icon small-jd">JD</span></div><div className={`account-status ${account?.logged_in ? 'ready' : ''}`}><span className="status-dot" /><strong>{account?.access_paused ? '访问保护已暂停' : account?.logged_in ? '登录会话已就绪' : account?.browser_open ? '等待完成登录' : '尚未连接账号'}</strong></div><p>{account?.message || '在电脑浏览器中登录京东，助手会记住这次会话。'}</p>{account?.browser_network && <p>独立浏览器网络：{account.browser_network === 'direct' ? '仅助手直连' : '跟随系统代理'}</p>}<button className="button secondary wide" disabled={pending === 'login' || !!overview?.busy_task} onClick={() => void loginBrowser()}>{pending === 'login' ? <Loader2 className="spin" size={15} /> : <ExternalLink size={15} />}{account?.access_paused ? '查看异常浏览器窗口' : account?.logged_in ? '重新打开登录浏览器' : '打开京东登录'}</button><div className="aside-note"><ShieldCheck size={13} />登录信息仅保存在本机</div></section>
          <section className="next-panel"><div className="next-top"><Clock3 size={16} /><span>下一场开售</span><span className="subtle-orbit" /></div>{nextTask ? <><h3>{nextTask.config.product.title}</h3><Countdown target={executionStart(nextTask.config)} now={clock} /><p>{stamp(nextTask.config.scheduled_at, true)} · 北京时间</p><button className="text-button" onClick={() => setDetailId(nextTask.id)}>查看任务 <ArrowRight size={14} /></button></> : <><div className="next-placeholder">好物，值得准时守候。</div><p>创建任务后，下个场次会显示在这里。</p><button className="text-button" onClick={() => { setEditing(null); setModal('create'); }}>安排一场抢购 <ArrowRight size={14} /></button></>}</section>
          <section className="panel phone-panel"><div className="phone-symbol"><Smartphone size={26} /><span><Wifi size={13} /></span></div><h3>电脑执行，手机掌握</h3><p>连接同一 Wi-Fi，用 iPhone 随时查看进度、处理任务。</p><button className="text-button" onClick={() => setModal('connect')}>连接手机控制台 <ArrowUpRight size={15} /></button></section>
          <div className="local-time"><span className="status-dot green" />电脑时间 · {stamp(new Date(clock).toISOString())}<span>Asia / Shanghai</span></div>
        </aside></div>
        <footer className="page-footer"><span>购物小助手 <i />让每一次准备，都有条不紊。</span><span>API 实验版 · v0.2.0-api.2</span></footer>
      </main>
    </div>
    {notice && <div className="toast" role="status"><Bell size={17} /><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice('')}><X size={15} /></button></div>}
    {modal === 'create' && <TaskForm now={clock} editing={editing} onClose={() => setModal(null)} onSaved={() => { setModal(null); void load(); setNotice('任务已保存。确认配置后点击启用，即可按场次自动提交订单。'); }} />}
    {modal === 'deleted' && <Modal title="已删除任务" subtitle="恢复只回到任务列表，不会自动启动或取消京东订单" onClose={() => setModal(null)}>{deletedTasks.length ? deletedTasks.map(task => <div className="deleted-task" key={task.id}><div><strong>{task.config.product.title}</strong><p>SKU {task.config.product.sku} · {labels[task.status]}</p></div><button className="button secondary mini" disabled={pending === task.id} onClick={async () => { setPending(task.id); try { await api(`/tasks/${task.id}/restore`, 'POST'); setDeletedTasks(items => items.filter(t => t.id !== task.id)); await load(); setNotice('任务已恢复，请检查场次后再启用。'); } catch (e) { setNotice((e as Error).message); } finally { setPending(''); } }}>恢复到列表</button></div>) : <p>没有已删除任务。</p>}</Modal>}
    {modal === 'connect' && <Modal title="连接手机控制台" subtitle="电脑和 iPhone 连接同一个 Wi-Fi" onClose={() => setModal(null)}><div className="qr-box">{overview?.lan_urls[0] ? <QRCodeSVG value={overview.lan_urls[0]} size={172} level="M" /> : <Wifi size={60} />}</div>{overview?.lan_urls.length ? overview.lan_urls.map(url => <div className="connection-url" key={url}><a href={url} target="_blank" rel="noreferrer">{url}</a><button className="icon-button" aria-label="复制连接地址" onClick={async () => { try { await navigator.clipboard.writeText(url); setNotice('地址已复制'); } catch { setNotice('请长按或选中上方地址复制'); } }}><Copy size={15} /></button></div>) : <p className="inline-error">未检测到局域网地址，请确认电脑已连接 Wi-Fi。</p>}<ol className="connection-steps"><li>用 iPhone 相机扫码，在 Safari 中打开。</li><li>输入电脑启动终端中显示的访问码。</li><li>保持电脑开机、联网和助手运行。</li></ol><p className="form-note">若无法连接，请检查 Wi-Fi 隔离和 Windows 防火墙的专用网络设置。手机关闭页面后，电脑仍会执行已启用任务。</p></Modal>}
    {modal === 'help' && <Modal title="把准备做好，准时开抢" subtitle="第一次使用，只需要这四步" onClose={() => setModal(null)}><div className="guide-list">{[['连接京东', '点击“打开京东登录”，在应用专用浏览器中扫码或完成登录。现有个人 Chrome 的登录与应用会话独立。'], ['确认好物与预算', '解析分享链接或 SKU，核对版本、规格、场次，填写最高应付总额和收货地址关键字。'], ['保存并启用', '先保存任务，再点击启用。助手检查预约并自动加购，开抢前准备结算，到点按提前量提交；未解锁商品到点继续检查。'], ['关注需要处理的状态', '遇到登录、验证码或页面变化，在电脑浏览器处理后恢复。结果不明时，恢复只核验订单。已下单后请手动付款。']].map(([title, text], i) => <div key={title}><span>{i + 1}</span><section><h3>{title}</h3><p>{text}</p></section></div>)}</div><p className="form-note">同一账号一次执行一个任务。页面提示“已适配”表示已实现适配器，具体商品是否能成功下单仍需真实场次验证。</p></Modal>}
    {resetTarget && <Modal title="重置到抢购前" subtitle="确认本次未生成订单后，重新安排抢购" onClose={() => { if (pending !== resetTarget.id) setResetTarget(null); }}><p>将检查京东订单列表。已有目标商品待付款订单或无法读取订单列表时，不会重置。</p><p>保留商品、预算、地址及时间配置；归档本次提交记录，清除待核验结果，回到停止状态。不会自动启动，也不会取消京东订单。</p><a href="https://order.jd.com/center/list.action" target="_blank" rel="noreferrer">打开京东订单列表 <ExternalLink size={13} /></a><label className="reset-confirm"><input type="checkbox" checked={resetConfirmed} disabled={pending === resetTarget.id} onChange={e => setResetConfirmed(e.target.checked)} />我已检查京东订单列表，确认本次没有生成订单</label>{resetError && <div className="inline-error" role="alert">{resetError}</div>}<div className="form-actions"><button className="button secondary" disabled={pending === resetTarget.id} onClick={() => setResetTarget(null)}>取消</button><button className="button primary" disabled={!resetConfirmed || !!overview?.busy_task || pending === resetTarget.id} onClick={async () => { const id = resetTarget.id; setPending(id); setResetError(''); try { await api(`/tasks/${id}/reset`, 'POST', { confirm_no_order: true }); setResetTarget(null); await load(); setDetailId(id); setNotice('已重置到抢购前，请检查或修改场次后再启用。'); } catch(e) { setResetError((e as Error).message); } finally { setPending(''); } }}>{pending === resetTarget.id ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />}{pending === resetTarget.id ? '检查订单中…' : '确认未下单并重置'}</button></div></Modal>}
    {selected && <Modal title="任务详情" subtitle={`京东 · SKU ${selected.config.product.sku}`} onClose={() => setDetailId(null)}><div className="detail-product"><span className="jd-icon">JD</span><div><h3>{selected.config.product.title}</h3><p>{selected.config.product.spec}</p></div></div><span className={`status-badge ${selected.status}`}><span className="status-dot" />{labels[selected.status]}</span><p className="detail-message">{selected.message || '任务待启用'}</p><dl className="detail-grid"><div><dt>开抢时间</dt><dd>{stamp(selected.config.scheduled_at, true)}（北京时间）</dd></div><div><dt>最高应付总额</dt><dd>¥{amount(selected.config.max_total_cents)}</dd></div><div><dt>提前量 / 持续时间</dt><dd>{selected.config.advance_ms || 0} 毫秒 / {selected.config.window_seconds} 秒</dd></div><div><dt>实际抢购区间</dt><dd>{exactStamp(executionStart(selected.config))} 至 {exactStamp(executionEnd(selected.config))}</dd></div><div><dt>地址核对关键字</dt><dd>{selected.config.address_hint}</dd></div>{selected.submission_order_id && <div><dt>提交接口返回订单号</dt><dd>{selected.submission_order_id}（待交叉核验）</dd></div>}</dl>{selected.result && <div className={`order-result ${selected.result.outcome}`}><strong>{selected.result.outcome === 'created' ? (selected.status === 'succeeded' ? '抢购成功，已锁单待付款' : '订单已创建，流程待确认') : '需要人工核对订单'}</strong><p>{selected.result.order_id ? `订单号：${selected.result.order_id}` : selected.result.message}</p>{selected.result.payment_entered && <p>已确认进入付款界面。</p>}{selected.result.payment_entered === false && <p>尚未确认进入付款界面，不能报告最终抢购成功。</p>}{selected.result.payment_flow === 'entered_and_exited' && <p>已进入并退出扫码支付，订单保留待付款。</p>}{selected.result.payment_flow === 'not_confirmed' && <p>订单已确认，支付页退出状态需人工核对。</p>}<a href="https://order.jd.com/center/list.action" target="_blank" rel="noreferrer">打开京东订单列表 <ExternalLink size={13} /></a></div>}{selected.screenshot && <a className="screenshot-link" href={`/api/tasks/${selected.id}/screenshot`} target="_blank" rel="noreferrer">查看最近一次页面截图 <ExternalLink size={14} /></a>}<div className="detail-actions">{active.has(selected.status) ? <button className="button secondary" onClick={() => void action(selected.id, 'stop')}><Pause size={15} />停止任务</button> : selected.status !== 'succeeded' && <button className="button primary" disabled={!!overview?.busy_task} onClick={() => void action(selected.id, selected.submit_intent || ['waiting_human', 'interrupted'].includes(selected.status) ? 'resume' : 'start')}><Play size={15} />{selected.submit_intent ? '仅核验订单' : ['waiting_human', 'interrupted'].includes(selected.status) ? '处理后恢复' : '启用任务'}</button>}{!active.has(selected.status) && !selected.submit_intent && <button className="button secondary" onClick={() => { setEditing(selected); setDetailId(null); setModal('create'); }}><Settings2 size={15} />修改配置</button>}{!active.has(selected.status) && selected.status !== 'succeeded' && !selected.submission_order_id && !selected.result?.order_id && selected.result?.outcome !== 'created' && <button className="button secondary" disabled={!!overview?.busy_task || pending === selected.id} onClick={() => { setResetTarget(selected); setResetConfirmed(false); setResetError(''); setDetailId(null); }}><RefreshCw size={15} />重置到抢购前</button>}<button className="button danger-quiet" disabled={pending === selected.id} onClick={() => void remove(selected)}><Trash2 size={15} />{pending === selected.id ? '处理中…' : '删除任务'}</button></div>{selected.submit_intent && selected.status !== 'succeeded' && <p className="form-note">该任务已有提交记录，恢复时仅查询订单。请核对京东订单列表后再安排新的购买任务。</p>}</Modal>}
  </div>;
}

function Modal({ title, subtitle, onClose, children }: { title: string; subtitle?: string; onClose: () => void; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    ref.current?.focus();
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      if (e.key === 'Tab') {
        const items = ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select,a[href],textarea');
        if (!items?.length) return;
        const first = items[0], last = items[items.length - 1];
        if (e.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener('keydown', handler);
    const old = document.body.style.overflow; document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', handler); document.body.style.overflow = old; previous?.focus(); };
  }, []);
  return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><div ref={ref} className="modal" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}><div className="modal-header"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" aria-label="关闭对话框" onClick={onClose}><X size={19} /></button></div><div className="modal-body">{children}</div></div></div>;
}

function TaskForm({ now, editing, onClose, onSaved }: { now: number; editing: Task | null; onClose: () => void; onSaved: () => void }) {
  const [source, setSource] = useState(editing?.config.product.sku || '');
  const [product, setProduct] = useState<Product | null>(editing?.config.product || null);
  const [parsedSource, setParsedSource] = useState(source);
  const [scheduled, setScheduled] = useState(editing ? chinaInput(editing.config.scheduled_at) : '');
  const [cap, setCap] = useState(editing ? (editing.config.max_total_cents / 100).toFixed(2) : '');
  const [address, setAddress] = useState(editing?.config.address_hint || '');
  const [windowSeconds, setWindowSeconds] = useState(editing?.config.window_seconds || 60);
  const [advanceMs, setAdvanceMs] = useState(editing?.config.advance_ms || 0);
  const [submitMode, setSubmitMode] = useState<'dom' | 'api'>(editing?.config.submit_mode || 'dom');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [probe, setProbe] = useState<{ reserved: boolean; reminder_set?: boolean; cart_found?: boolean; cart_quantity?: number | null; cart_selectable: boolean; order_list_readable: boolean } | null>(null);
  const parse = async (value = source) => {
    setSource(value); setBusy('parse'); setError(''); setProduct(null); setProbe(null);
    try { const result = await api<Product>('/products/resolve', 'POST', { source: value }); setProduct(result); setParsedSource(value); if (result.sale_start && !scheduled) setScheduled(chinaInput(result.sale_start)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(''); }
  };
  const validTiming = !!scheduled && Number.isFinite(Date.parse(`${scheduled}+08:00`)) && Number.isInteger(advanceMs) && advanceMs >= 0 && advanceMs <= 86400000 && Number.isInteger(windowSeconds) && windowSeconds >= 1 && windowSeconds <= 86400;
  return <Modal title={editing ? '修改抢购任务' : '创建抢购任务'} subtitle="先确认商品与预算，再安排你的开售场次" onClose={onClose}><form onSubmit={async e => { e.preventDefault(); if (!product || source !== parsedSource) { setError('请先解析并确认商品'); return; } if (new Date(`${scheduled}+08:00`).getTime() - advanceMs <= now) { setError('实际抢购开始时间已过去，请调整开始时间或提前量'); return; } if (!/^\d+(?:\.\d{1,2})?$/.test(cap)) { setError('金额最多填写两位小数'); return; } setBusy('save'); setError(''); try { const config: Config = { product, quantity: 1, scheduled_at: `${scheduled.length === 16 ? scheduled + ":00" : scheduled}+08:00`, max_total_cents: Math.round(Number(cap) * 100), address_hint: address, window_seconds: windowSeconds, advance_ms: advanceMs, submit_mode: submitMode }; await api(editing ? `/tasks/${editing.id}` : '/tasks', editing ? 'PUT' : 'POST', config); onSaved(); } catch (e) { setError((e as Error).message); } finally { setBusy(''); } }}>
    <label htmlFor="product-source">商品链接 / 京东分享文案 / SKU</label><div className="source-input"><Link2 size={17} /><input id="product-source" value={source} onChange={e => setSource(e.target.value)} placeholder="粘贴心仪商品的链接或编号" required /><button type="button" className="button primary mini" disabled={!!busy || !source.trim()} onClick={() => void parse()}>{busy === 'parse' ? <Loader2 className="spin" size={14} /> : '解析'}</button></div>
    {!product && <button className="example-product" type="button" disabled={!!busy} onClick={() => void parse('100327520619')}><span className="jd-icon small-jd">JD</span><span>试试：雷鸟 GT Max bilibili 限定版 M 码</span><ArrowUpRight size={13} /></button>}
    {product && source === parsedSource && <div className="resolved-product"><span className="product-verified"><CheckCircle2 size={14} />已解析京东商品</span><h3>{product.title}</h3><p>{product.spec || '按当前 SKU 精确购买'} · SKU {product.sku}</p><span className="resolved-tag">{product.reservation_required ? '预约后限时抢购' : '普通商品定时下单'}</span>{product.sale_start && <p>页面开售：{stamp(product.sale_start, true)} <button type="button" className="text-button" onClick={() => setScheduled(chinaInput(product.sale_start!))}>使用此时间</button></p>}<a href={product.url} target="_blank" rel="noreferrer">核对商品 <ExternalLink size={12} /></a><div className="probe-row"><button className="text-button" type="button" disabled={!!busy} onClick={async () => { setBusy('probe'); setError(''); try { setProbe(await api('/products/preflight', 'POST', {source: product.sku})); } catch(e) { setError((e as Error).message); } finally { setBusy(''); } }}>{busy === 'probe' ? <Loader2 size={13} className="spin" /> : <ShieldCheck size={13} />}检查网页准备</button>{probe && <p>预约：{probe.reserved ? '资格已确认' : probe.reminder_set ? '开抢提醒已设置（资格待结算校验）' : '尚未确认'} · 购物车：{probe.cart_found === false ? '尚未加购（启用时自动加入）' : probe.cart_selectable ? '可勾选' : '尚未解锁'} · 数量：{probe.cart_found === false ? '加购后核对' : probe.cart_quantity ?? '未确认'} · 订单列表：{probe.order_list_readable ? '可读取' : '待校准'}</p>}</div></div>}
    <div className="form-two"><div><label htmlFor="sale-time">开抢时间 <span>北京时间</span></label><input id="sale-time" type="datetime-local" step="1" value={scheduled} onChange={e => setScheduled(e.target.value)} required /></div><div><label htmlFor="cap">最高应付总额 <span>含运费</span></label><div className="money-input"><span>¥</span><input id="cap" type="number" step="0.01" min="0.01" max="1000000" inputMode="decimal" value={cap} onChange={e => setCap(e.target.value)} placeholder="填写可接受的最高金额" required /></div></div></div>
    <label htmlFor="submit-mode">提交方式</label><select id="submit-mode" value={submitMode} onChange={e => setSubmitMode(e.target.value as 'dom' | 'api')}><option value="dom">页面提交（默认）</option><option value="api">API 下单（实验）</option></select>{submitMode === 'api' && <p className="field-hint">实验模式仍需网页核验与手动扫码付款。仅发送一次请求，失败不自动回退；首次建议用普通商品测试，不保证更快或免风控。</p>}
    <label htmlFor="address">收货地址核对关键字</label><input id="address" value={address} onChange={e => setAddress(e.target.value)} minLength={2} maxLength={120} placeholder="填写京东默认收货地址中的街道或小区名" required /><p className="field-hint">结算页选中地址必须包含此关键字，否则助手会暂停。</p>
    <div className="form-two"><div><label>购买数量</label><div className="readonly-input">1 件 <span>第一版单件购买</span></div></div><div><label htmlFor="advance">提前抢购时间（毫秒）</label><input id="advance" type="number" min="0" max="86400000" step="1" required value={advanceMs} onChange={e => setAdvanceMs(Number(e.target.value))} /></div></div><div className="form-two"><div><label htmlFor="window">持续抢购时间（秒）</label><input id="window" type="number" min="1" max="86400" step="1" required value={windowSeconds} onChange={e => setWindowSeconds(Number(e.target.value))} /></div><div className="field-hint">按提前量计算提交时刻；提前 20 秒开始结算准备，目标提前 10 秒就绪。持续时间从实际开始计算。</div></div>
    {validTiming && <p className="timing-preview">实际开始：{exactStamp(executionStart({ scheduled_at: `${scheduled}+08:00`, advance_ms: advanceMs, window_seconds: windowSeconds } as Config))}<br />截止：{exactStamp(executionEnd({ scheduled_at: `${scheduled}+08:00`, advance_ms: advanceMs, window_seconds: windowSeconds } as Config))}（北京时间）</p>}
    <div className="form-info"><ShieldCheck size={17} /><p>保存后为待启用状态。启用后检查预约、自动加购，在实际开始前 20 秒尝试准备结算，目标提前 10 秒完成；到点按预算提交后进入付款界面，锁单并核对订单号。付款由你完成。</p></div>{error && <div className="inline-error" role="alert">{error}</div>}<div className="form-actions"><button className="button secondary" type="button" onClick={onClose}>取消</button><button className="button primary" type="submit" disabled={!!busy || !product || source !== parsedSource}>{busy === 'save' ? <Loader2 className="spin" size={16} /> : <Check size={16} />}保存任务</button></div>
  </form></Modal>;
}

createRoot(document.getElementById('root')!).render(<App />);
