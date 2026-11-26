/**
 * dsh-designer-kit —— 宿主半边。
 *
 * 只做一件事：把本机 designer-kit/workbench 的 Python 事件层命令
 * 暴露成 5 个 DSH 工具。不改写业务逻辑，不缓状态；每次调用都是一次
 * 真实 CLI 执行，输出原样返回（含错误），失败绝不美化。
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const name = 'designer-kit'
export const inject = { tools: { required: true }, commands: { required: false }, systemPrompt: { required: false } }

const HOME = process.env.DESIGNER_KIT_HOME || join(homedir(), 'designer-kit')
const WORKBENCH = join(HOME, 'workbench')
const PYTHON = process.env.DESIGNER_KIT_PYTHON || 'python3'
const TIMEOUT_MS = 30_000
const MAX_OUTPUT = 20_000

const identifier = { type: 'string', minLength: 1, maxLength: 200 }
const input = (properties, required = Object.keys(properties)) => ({
  type: 'object', properties, required, additionalProperties: false,
})
const output = {
  schema: input({ text: { type: 'string' }, ok: { type: 'boolean' } }),
  render: (_args, value) => [{ type: 'text', text: value?.text ?? '' }],
}

function run(args) {
  return new Promise((resolve) => {
    if (!existsSync(join(WORKBENCH, 'bin'))) {
      return resolve({ ok: false, text: `找不到事件层：${WORKBENCH}/bin。请设置 DESIGNER_KIT_HOME 指向仓库根目录。` })
    }
    const child = spawn(PYTHON, args, { cwd: WORKBENCH, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } })
    let out = ''
    let err = ''
    let done = false
    const finish = (value) => { if (!done) { done = true; resolve(value) } }
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      finish({ ok: false, text: `命令超时（${TIMEOUT_MS / 1000}s）：${args.join(' ')}` })
    }, TIMEOUT_MS)
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => { if (out.length < MAX_OUTPUT) out += chunk })
    child.stderr.on('data', (chunk) => { if (err.length < MAX_OUTPUT) err += chunk })
    child.on('error', (error) => {
      clearTimeout(timer)
      finish({ ok: false, text: `无法启动 ${PYTHON}：${error.message}` })
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      const body = [out.trim(), err.trim()].filter(Boolean).join('\n')
      finish({ ok: code === 0, text: body || `（无输出，退出码 ${code}）` })
    })
  })
}

function requireProject(args) {
  if (!args.project) throw new Error('缺少 project：请给出项目名（assets/ 下的目录名）')
  return String(args.project)
}

export function apply(ctx) {
  if (typeof ctx.tools?.register !== 'function') return
  const register = (definition, label) => ctx.effect(() => ctx.tools.register(definition), label)

  register({
    name: 'designer_kit_today',
    description: '一天一条：汇总待确认候选、待处理提案、解析失败的文件。只读，不写业务事件。',
    parameters: input({ project: identifier, all: { type: 'boolean' } }, []),
    output,
    async execute(args) {
      const argv = ['bin/day.py']
      if (args.all) argv.push('--all')
      else argv.push(requireProject(args))
      argv.push('--scan', '--dry')
      return run(argv)
    },
  }, 'designer-kit: today')

  register({
    name: 'designer_kit_events',
    description: '读取项目事件流（最近 N 条）。事件层是唯一真源，状态由它投影出来。',
    parameters: input({ project: identifier, limit: { type: 'number', minimum: 1, maximum: 500 } }),
    output,
    async execute(args) {
      return run(['bin/eventlog.py', 'log', '--project', requireProject(args), '--limit', String(args.limit ?? 20)])
    },
  }, 'designer-kit: events')

  register({
    name: 'designer_kit_proposals',
    description: '列出触发引擎产出的提案（含复发次数与上次处理结果）。只读。',
    parameters: input({ project: identifier, all: { type: 'boolean' } }),
    output,
    async execute(args) {
      const argv = ['bin/trigger.py', 'list', '--project', requireProject(args)]
      if (args.all) argv.push('--all')
      return run(argv)
    },
  }, 'designer-kit: proposals')

  register({
    name: 'designer_kit_record',
    description: '记录一次条件变更：写 condition.fixed/changed 事件并自动算出下游返工。必须给 evidence（文件:行号 / 邮件 id / 会议时间戳 / 复核单号），没出处会被拒绝。',
    parameters: input({
      project: identifier, condition: identifier, value: identifier,
      evidence: { type: 'string', minLength: 1, maxLength: 500 },
      reason: { type: 'string', maxLength: 500 },
    }),
    output,
    async execute(args) {
      const argv = ['bin/eventlog.py', 'set', String(args.condition), String(args.value),
        '--project', requireProject(args), '--evidence', String(args.evidence)]
      if (args.reason) argv.push('--reason', String(args.reason))
      return run(argv)
    },
  }, 'designer-kit: record')

  register({
    name: 'designer_kit_verify',
    description: '体检：重放事件与投影比对，并逐条校验哈希链（改过历史会报出来）。只读。',
    parameters: input({ project: identifier }),
    output,
    async execute(args) {
      return run(['bin/eventlog.py', 'verify', '--project', requireProject(args)])
    },
  }, 'designer-kit: verify')
}
