window.__ModuleLoader__.load({
  id: 'dsh-bundle-docker-upgrade',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const installer = 'https://github.com/compilink/dsh-docker-upgrade-plugin/releases/download/v0.2.0/DSH-Docker-Updater-macOS.zip'
    const labels = {
      idle: '尚未执行', checking: '正在检查远程源码', building: '正在构建 Docker 镜像',
      validating: '正在验证候选版本', 'backing-up': '正在备份数据', deploying: '正在部署容器',
      verifying: '正在验证新容器', failed: '执行失败', blocked: '本次执行已暂停', 'rolled-back': '已回滚',
    }

    async function request(suffix, revision, signal) {
      const response = await fetch(new URL('local/dsh-docker-upgrade-' + suffix, document.baseURI), {
        method: revision ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store', signal,
        headers: revision ? { 'X-DSH-Upgrade-Revision': revision } : {},
      })
      const result = await response.json().catch(() => ({}))
      if (!response.ok) {
        const error = new Error(result.error || ([401, 403].includes(response.status) ? '请在本机以 DSH 所有者身份登录。' : '暂时无法连接 DSH，将自动重试'))
        error.status = response.status
        throw error
      }
      return result
    }

    function describe(status) {
      if (status.phase !== 'succeeded') return labels[status.phase] || status.phase
      if (status.action === 'check') return status.available === null ? '已读取上游版本' : status.available ? '发现更新' : '已是最新版本'
      return '升级检查已完成'
    }

    function Panel() {
      const [state, setState] = React.useState(null)
      const [error, setError] = React.useState('')
      const [posting, setPosting] = React.useState(false)
      const [connected, setConnected] = React.useState(false)
      const [connectionError, setConnectionError] = React.useState('')
      const controller = React.useRef(null)
      const refreshRef = React.useRef(null)
      const submitting = React.useRef(false)

      React.useEffect(() => {
        const lifetime = new AbortController()
        controller.current = lifetime
        let poll = 0
        async function refresh() {
          const n = ++poll
          try {
            const data = await request('status', null, AbortSignal.any([lifetime.signal, AbortSignal.timeout(8000)]))
            if (lifetime.signal.aborted || n !== poll) return
            setState(data)
            setConnected(true)
            setConnectionError('')
          } catch (err) {
            if (lifetime.signal.aborted || n !== poll) return
            setConnected(false)
            if (err.status === 401 || err.status === 403) setState(null)
            setConnectionError(err instanceof TypeError || err.name === 'TimeoutError' ? '暂时无法连接 DSH，正在重连。' : err.message)
          }
        }
        refreshRef.current = refresh
        let timer
        async function pollAgain() {
          await refresh()
          if (!lifetime.signal.aborted) timer = setTimeout(pollAgain, 5000)
        }
        void pollAgain()
        return () => { lifetime.abort(); clearTimeout(timer) }
      }, [])

      async function submit(action) {
        if (submitting.current || !connected || !state?.workerOnline || state.busy) return
        if (action === 'run' && (state.blocker || !window.confirm('检查远程源码并升级本机 DSH？部署时网页连接会短暂中断。'))) return
        submitting.current = true
        setPosting(true)
        setError('')
        const lifetime = controller.current.signal
        try {
          await request(action, state.revision, AbortSignal.any([lifetime, AbortSignal.timeout(10000)]))
        } catch (err) {
          if (!lifetime.aborted) setError(err instanceof TypeError || err.name === 'TimeoutError' ? '未收到提交结果。请查看下方执行状态。' : err.message)
        } finally {
          if (!lifetime.aborted) {
            await refreshRef.current()
            if (!lifetime.aborted) setPosting(false)
          }
          submitting.current = false
        }
      }
      const status = state?.status
      const mountNames = { bind: 'Mac 上的目录', volume: 'Docker 命名卷' }
      const unavailable = !connected || !state?.workerOnline || state.busy || posting
      return h('section', { style: { maxWidth: '42rem', padding: '1rem', display: 'grid', gap: '0.75rem' } },
        h('h2', null, 'DSH Docker 升级'),
        state?.mounts && h('p', null, '跟踪 DeepSeek Harness 官方 GitHub 默认分支。DSH 数据使用' +
          mountNames[state.mounts.data] + '，工作区使用' + mountNames[state.mounts.workspace] + '。'),
        connected && state?.workerState === 'missing' && h('div', null,
          h('p', null, '尚未检测到宿主升级服务。在运行 Docker Desktop 的 Mac 上下载安装包，解压后双击安装器，确认它识别出的 DSH 部署。'),
          h('a', { href: installer, download: '', target: '_blank', rel: 'noopener noreferrer' }, '安装 Mac 宿主升级服务'),
          h('p', null, '安装完成后，本页会自动显示服务状态。')),
        state?.scheduleTime && h('p', null, '宿主机每天 ' + state.scheduleTime + ' 检查并自动部署更新' +
          (state.nextCheckAt ? '。下次检查 ' + new Date(state.nextCheckAt).toLocaleString() : '')),
        h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '0.5rem' } },
          h('button', { type: 'button', disabled: unavailable, onClick: () => submit('check') }, '检查更新'),
          h('button', { type: 'button', disabled: unavailable || !!state?.blocker, onClick: () => submit('run') }, '立即升级')),
        h('div', { role: 'status', 'aria-live': 'polite' },
          !connected && h('p', null, connectionError || '正在连接本机 DSH。'),
          connected && state?.workerState === 'online' && h('p', null, '宿主升级服务已连接。'),
          connected && state?.workerState === 'stale' && h('p', null, '已检测到宿主服务的运行记录，但尚未收到新的心跳。请确认 Docker 主机已唤醒、服务正在运行。'),
          connected && state?.workerState === 'blocked' && h('p', null, '宿主升级服务已连接，当前暂停处理请求。请查看下方执行结果。'),
          connected && !state?.workerState && !state?.workerOnline && h('p', null, '宿主机升级服务未就绪，请检查服务状态。'),
          state?.pending && h('p', null, state.pending === 'upgrade' ? '升级请求已提交，等待宿主机处理。' : '检查请求已提交，等待宿主机处理。'),
          state?.blocker && h('p', null, state.blocker),
          status && h('div', null,
            h('p', null, (status.source === 'scheduled' ? '定时任务 · ' : status.source === 'manual' ? '手动任务 · ' : '') + describe(status) + ' · ' + new Date(status.updatedAt).toLocaleString()),
            status.message && h('p', null, status.message),
            status.current && h('p', null, '本地提交 ', h('code', null, status.current.slice(0, 12))),
            status.latest && h('p', null, '上游提交 ', h('code', null, status.latest.slice(0, 12))))),
        error && h('p', { role: 'alert' }, error))
    }
    return {
      name: 'dsh-bundle-docker-upgrade', inject: ['slots'],
      apply(ctx) {
        ctx.effect(() => ctx.slots.inject('settings.section', () => ctx.slots.register({
          name: 'settings.section', id: 'docker-upgrade', order: 50, label: () => 'DSH Docker 升级', inject: () => ({}),
        }, Panel)))
      },
    }
  },
})
