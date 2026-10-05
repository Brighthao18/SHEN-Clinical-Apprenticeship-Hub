"use client";

import { Archive, BookOpenText, BrainCircuit, Eye, FileScan, History, KeyRound, LoaderCircle, Power, Save, ShieldAlert, ShieldCheck, Trash2 } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type SlotKey =
  | "clinical_extraction"
  | "vision_verification"
  | "longitudinal_analysis"
  | "apprenticeship_writing"
  | "mineru_ocr";

type ProviderSettingView = {
  slot: string;
  configured: boolean;
  adapter: string | null;
  base_url: string | null;
  model: string | null;
  enabled: boolean;
  confirmed_identifiable_upload: boolean;
  api_key_masked: string | null;
};

type Preset = { label: string; base_url: string; model: string; note: string };

const SLOT_META: Record<
  SlotKey,
  {
    adapter: string;
    title: string;
    blurb: string;
    confirmText: string;
    savedNote: string;
    clearedNote: string;
    modelLabel?: string;
    apiKeyLabel?: string;
    apiKeyPlaceholder?: string;
    presets: Record<string, Preset>;
  }
> = {
  clinical_extraction: {
    adapter: "live_openai_compatible",
    title: "结构化抽取 Provider",
    blurb:
      "ClinicalAIExtractor 使用该 Provider 把 OCR 文本结构化为临床事实。密钥加密保存于本机数据库，不会回传到页面；可随时更换或清除。",
    confirmText:
      "我确认理解：真实病历 OCR 文本包含姓名/病历号等标识，将被发送到上述 Provider 用于结构化抽取；该行为会被记录到审计日志（不含密钥与病历内容）。",
    savedNote: "已保存。抽取管线会立即使用该 Provider。",
    clearedNote: "已清除。抽取回到 mock/fail-closed 模式。",
    presets: {
      zhipu: {
        label: "智谱 GLM",
        base_url: "https://open.bigmodel.cn/api/paas/v4",
        model: "glm-4.7",
        note: "云端；OCR 原文将发送至智谱。",
      },
      deepseek: {
        label: "DeepSeek",
        base_url: "https://api.deepseek.com",
        model: "deepseek-chat",
        note: "云端；OCR 原文将发送至 DeepSeek。",
      },
      openai: {
        label: "OpenAI",
        base_url: "https://api.openai.com/v1",
        model: "gpt-4.1",
        note: "云端；OCR 原文将发送至 OpenAI。",
      },
      ollama: {
        label: "本地 Ollama（隐私最优）",
        base_url: "http://127.0.0.1:11434/v1",
        model: "qwen2.5:14b",
        note: "本机推理；数据不出机器。需先安装并拉取模型。",
      },
    },
  },
  vision_verification: {
    adapter: "live_openai_compatible",
    title: "视觉核验 Provider（Vision Verification Fallback）",
    blurb:
      "仅当 Validator 判定错误可能来自 OCR 缺损（如包装单位/病历号缺失）时，系统会把原始病历图片发送到该 Provider 做一次受约束的字段核验；读不清必须返回空，不会猜测。需后端同时开启 AI_VISION_VERIFICATION_ENABLED 才会生效。须选择支持图片输入的模型。",
    confirmText:
      "我确认理解：原始病历图片包含姓名/病历号等标识，将在上述受限场景被发送到该 Provider 做字段核验；该行为连同图片哈希会被记录到审计日志（不含密钥与病历内容）。",
    savedNote: "已保存。视觉核验会在触发时使用该 Provider。",
    clearedNote: "已清除。视觉核验回到不可用（文档保持 UNRESOLVED，安全默认）。",
    presets: {
      zhipu_vision: {
        label: "智谱 GLM 视觉",
        base_url: "https://open.bigmodel.cn/api/paas/v4",
        model: "glm-4.6v",
        note: "云端；病历图片将发送至智谱。",
      },
      openai_vision: {
        label: "OpenAI 视觉",
        base_url: "https://api.openai.com/v1",
        model: "gpt-4.1",
        note: "云端；病历图片将发送至 OpenAI。",
      },
      ollama_vision: {
        label: "本地 Ollama 视觉（隐私最优）",
        base_url: "http://127.0.0.1:11434/v1",
        model: "qwen2.5vl:7b",
        note: "本机推理；图片不出机器。需先拉取视觉模型。",
      },
    },
  },
  longitudinal_analysis: {
    adapter: "live_openai_compatible",
    title: "患者纵向分析 Provider",
    blurb:
      "仅接收系统生成的去标识化纵向病程上下文与 Phase 7 确定性处方 Diff，用于生成结构化、可追溯的 AI 推测。不会接收姓名、病历号、原始图片或未过滤 OCR。建议配置较强的文本模型。",
    confirmText:
      "我确认理解：去标识化的临床事实、学生补录与确定性处方变化将被发送到上述 Provider；系统会在发送前再次执行标识符检查，并保存模型、提示词版本与上下文哈希。",
    savedNote: "已保存。患者纵向分析会使用该独立 Provider。",
    clearedNote: "已清除。纵向分析将停止外部调用，既有版本仍保留。",
    presets: {
      zhipu: {
        label: "智谱 GLM",
        base_url: "https://open.bigmodel.cn/api/paas/v4",
        model: "glm-4.7",
        note: "云端；发送去标识化纵向上下文。",
      },
      deepseek: {
        label: "DeepSeek",
        base_url: "https://api.deepseek.com",
        model: "deepseek-chat",
        note: "云端；发送去标识化纵向上下文。",
      },
      openai: {
        label: "OpenAI",
        base_url: "https://api.openai.com/v1",
        model: "gpt-4.1",
        note: "云端；发送去标识化纵向上下文。",
      },
      ollama: {
        label: "本地 Ollama（隐私最优）",
        base_url: "http://127.0.0.1:11434/v1",
        model: "qwen2.5:14b",
        note: "本机推理；数据不出机器。需先安装并拉取模型。",
      },
    },
  },
  apprenticeship_writing: {
    adapter: "live_openai_compatible",
    title: "师承写作 Provider（跟师笔记 / 医案整理）",
    blurb:
      "仅接收系统生成的去标识化就诊事实、学生补录与 Phase 7 确定性处方 Diff，用于产出可追溯的写作草稿。不会接收姓名、病历号或原始图片；AI 输出永远是草稿，最终稿由用户定稿。建议配置较强的中文文本模型。",
    confirmText:
      "我确认理解：去标识化的临床事实、学生补录与确定性处方变化将被发送到上述 Provider 用于生成写作草稿；系统会在发送前再次执行标识符检查，并保存模型、提示词版本与上下文哈希。",
    savedNote: "已保存。跟师笔记与医案整理会使用该独立 Provider。",
    clearedNote: "已清除。师承写作将停止外部调用，既有笔记版本仍保留。",
    presets: {
      zhipu: {
        label: "智谱 GLM",
        base_url: "https://open.bigmodel.cn/api/paas/v4",
        model: "glm-4.7",
        note: "云端；发送去标识化就诊上下文。",
      },
      deepseek: {
        label: "DeepSeek",
        base_url: "https://api.deepseek.com",
        model: "deepseek-chat",
        note: "云端；发送去标识化就诊上下文。",
      },
      openai: {
        label: "OpenAI",
        base_url: "https://api.openai.com/v1",
        model: "gpt-4.1",
        note: "云端；发送去标识化就诊上下文。",
      },
      ollama: {
        label: "本地 Ollama（隐私最优）",
        base_url: "http://127.0.0.1:11434/v1",
        model: "qwen2.5:14b",
        note: "本机推理；数据不出机器。需先安装并拉取模型。",
      },
    },
  },
  mineru_ocr: {
    adapter: "mineru_cloud_api",
    title: "MinerU OCR Provider（云端 OCR）",
    blurb:
      "MinerU 负责把原始病历文件解析为 OCR 文本。这是独立的隐私边界：原始文件（含姓名/病历号）会上传到 MinerU 云端。密钥加密保存于本机数据库；实际启用仍需后端 OCR_PROVIDER=mineru 且 ALLOW_IDENTIFIABLE_MINERU_UPLOAD=true。",
    confirmText:
      "我确认理解：原始病历文件包含姓名/病历号等标识，将在 OCR 时上传到上述 MinerU 服务；该行为会被记录到审计日志（不含密钥与病历内容）。",
    savedNote:
      "已保存。OCR 运行时将使用该配置（若环境变量 MINERU_API_TOKEN 已设置，则环境变量优先）。",
    clearedNote: "已清除。OCR 回到环境变量配置；两者都缺失时 OCR 失败（fail-closed）。",
    modelLabel: "模型版本（model_version）",
    apiKeyLabel: "API Token",
    apiKeyPlaceholder: "MinerU API Token（mineru.net 申请）",
    presets: {
      mineru_vlm: {
        label: "MinerU 云端 VLM",
        base_url: "https://mineru.net",
        model: "vlm",
        note: "官方云端 VLM 解析；原始文件将上传至 MinerU。",
      },
      mineru_pipeline: {
        label: "MinerU 云端 Pipeline",
        base_url: "https://mineru.net",
        model: "pipeline",
        note: "官方云端经典管线；原始文件将上传至 MinerU。",
      },
    },
  },
};

const SLOT_KEYS = Object.keys(SLOT_META) as SlotKey[];

const TAB_LABEL: Record<SlotKey, string> = {
  clinical_extraction: "结构化抽取",
  vision_verification: "视觉核验",
  longitudinal_analysis: "患者纵向分析",
  apprenticeship_writing: "师承写作",
  mineru_ocr: "MinerU OCR",
};

function slotDotClass(view: ProviderSettingView | undefined) {
  if (!view?.configured) return "bg-stone-300";
  return view.enabled ? "bg-emerald-500" : "bg-amber-500";
}

function initialSlot(): SlotKey {
  if (typeof window !== "undefined") {
    const hash = window.location.hash.replace("#", "");
    if ((SLOT_KEYS as string[]).includes(hash)) return hash as SlotKey;
  }
  return SLOT_KEYS[0];
}

function ProviderCard({ slot, view, onChanged }: {
  slot: SlotKey;
  view: ProviderSettingView | null;
  onChanged: (view: ProviderSettingView | null) => void;
}) {
  const meta = SLOT_META[slot];
  const firstPreset = Object.entries(meta.presets)[0];
  // 卡片在配置读取完成后才由父组件挂载，因此用惰性初始化直接回显
  // 当前配置；预设按钮只在 base_url+model 完全匹配时高亮。
  const configuredView = view?.configured ? view : null;
  const [adapter, setAdapter] = useState<string>(() => {
    if (!configuredView) return firstPreset[0];
    const match = Object.entries(meta.presets).find(
      ([, preset]) =>
        preset.base_url === configuredView.base_url && preset.model === configuredView.model,
    );
    return match ? match[0] : "";
  });
  const [baseUrl, setBaseUrl] = useState(configuredView?.base_url ?? firstPreset[1].base_url);
  const [model, setModel] = useState(configuredView?.model ?? firstPreset[1].model);
  const [apiKey, setApiKey] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function applyPreset(key: string) {
    setAdapter(key);
    setBaseUrl(meta.presets[key].base_url);
    setModel(meta.presets[key].model);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await apiFetch<ProviderSettingView>("/api/v1/settings/ai-provider", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          slot,
          adapter: meta.adapter,
          base_url: baseUrl,
          model,
          api_key: apiKey,
          confirmed_identifiable_upload: confirmed,
        }),
      });
      const keptKey = view?.configured && apiKey.trim() === "";
      onChanged(updated);
      setApiKey("");
      setConfirmed(false);
      setNotice(keptKey ? `${meta.savedNote}已保留原有密钥。` : meta.savedNote);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled() {
    if (!view?.configured) return;
    setError("");
    setNotice("");
    setToggling(true);
    try {
      const updated = await apiFetch<ProviderSettingView>("/api/v1/settings/ai-provider", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slot, enabled: !view.enabled }),
      });
      onChanged(updated);
      setNotice(
        updated.enabled
          ? "已启用。运行时恢复使用该 Provider。"
          : "已停用。配置保留，运行时立即回退（环境变量或 fail-closed）。",
      );
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setToggling(false);
    }
  }

  async function clearProvider() {
    setError("");
    setNotice("");
    setClearing(true);
    try {
      await apiFetch(`/api/v1/settings/ai-provider?slot=${slot}`, { method: "DELETE" });
      onChanged(null);
      setBaseUrl(firstPreset[1].base_url);
      setModel(firstPreset[1].model);
      setAdapter(firstPreset[0]);
      setApiKey("");
      setConfirmed(false);
      setConfirmingClear(false);
      setNotice(meta.clearedNote);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setClearing(false);
    }
  }

  return (
    <section aria-label={meta.title} className="surface mt-6 rounded-2xl p-5 sm:p-6">
      <div className="flex items-center gap-3">
        <span className="grid size-10 place-items-center rounded-xl bg-[#dcebe5] text-[#1f6857]">
          {slot === "vision_verification" ? (
            <Eye aria-hidden="true" size={19} />
          ) : slot === "longitudinal_analysis" ? (
            <BrainCircuit aria-hidden="true" size={19} />
          ) : slot === "apprenticeship_writing" ? (
            <BookOpenText aria-hidden="true" size={19} />
          ) : slot === "mineru_ocr" ? (
            <FileScan aria-hidden="true" size={19} />
          ) : (
            <KeyRound aria-hidden="true" size={19} />
          )}
        </span>
        <div>
          <h2 className="text-sm font-semibold text-stone-800">{meta.title}</h2>
          <p className="mt-0.5 text-xs leading-5 text-stone-500">{meta.blurb}</p>
        </div>
      </div>

      <div className="mt-4 rounded-xl bg-stone-50 px-4 py-3 text-sm">
        {view?.configured ? (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="flex items-center gap-2 font-semibold text-stone-800">
                <span
                  aria-hidden="true"
                  className={`size-2 rounded-full ${view.enabled ? "bg-emerald-500" : "bg-amber-500"}`}
                />
                {view.enabled ? "已启用" : "已停用（配置保留）"}
              </p>
              <button
                type="button"
                onClick={toggleEnabled}
                disabled={toggling || saving || clearing}
                className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-stone-300 px-3 py-1.5 text-xs font-semibold text-stone-700 hover:border-stone-400 disabled:opacity-60"
              >
                {toggling ? (
                  <LoaderCircle aria-hidden="true" className="animate-spin" size={13} />
                ) : (
                  <Power aria-hidden="true" size={13} />
                )}
                {view.enabled ? "停用" : "重新启用"}
              </button>
            </div>
            <p className="mt-0.5 text-xs text-stone-500">
              {view.base_url} · {view.model} · 密钥 {view.api_key_masked}
            </p>
          </>
        ) : (
          <>
            <p className="font-semibold text-stone-800">未配置</p>
            <p className="mt-0.5 text-xs text-stone-500">
              {slot === "vision_verification"
                ? "未配置时，OCR 缺损的病历将安全地保持 UNRESOLVED，不会触发图片外发。"
                : slot === "longitudinal_analysis"
                  ? "未配置时不能生成新纵向分析；既有版本不受影响。"
                  : slot === "apprenticeship_writing"
                    ? "未配置时不能生成新的跟师笔记/医案草稿；既有笔记版本不受影响。"
                    : slot === "mineru_ocr"
                      ? "未配置时 OCR 使用环境变量 MINERU_API_TOKEN；两者都缺失时 OCR 失败（fail-closed）。"
                      : "未配置时抽取处于 mock / fail-closed 模式。"}
            </p>
          </>
        )}
      </div>

      <form onSubmit={save} className="mt-5 space-y-5">
        <div>
          <p className="text-sm font-semibold text-stone-700">选择 Provider</p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {Object.entries(meta.presets).map(([key, preset]) => (
              <button
                key={key}
                type="button"
                onClick={() => applyPreset(key)}
                className={`focus-ring rounded-xl border px-4 py-3 text-left text-sm ${
                  adapter === key
                    ? "border-[#1f6857] bg-[#eef6f2] font-semibold text-[#1f6857]"
                    : "border-stone-200 bg-white text-stone-700 hover:border-stone-300"
                }`}
              >
                {preset.label}
                <span className="mt-0.5 block text-xs font-normal text-stone-500">{preset.note}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block text-sm font-semibold text-stone-700">
            Base URL
            <input
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              required
              className="focus-ring mt-1.5 w-full rounded-xl border border-stone-300 bg-white px-3 py-2.5 text-sm font-normal text-stone-800"
            />
          </label>
          <label className="block text-sm font-semibold text-stone-700">
            {meta.modelLabel ?? "模型"}
            <input
              value={model}
              onChange={(event) => setModel(event.target.value)}
              required
              className="focus-ring mt-1.5 w-full rounded-xl border border-stone-300 bg-white px-3 py-2.5 text-sm font-normal text-stone-800"
            />
          </label>
        </div>

        <label className="block text-sm font-semibold text-stone-700">
          {meta.apiKeyLabel ?? "API Key"}
          <input
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            required={!view?.configured}
            autoComplete="off"
            placeholder={
              view?.configured
                ? "留空保留已保存的 Key；输入则替换"
                : (meta.apiKeyPlaceholder ?? "sk-…")
            }
            className="focus-ring mt-1.5 w-full rounded-xl border border-stone-300 bg-white px-3 py-2.5 text-sm font-normal text-stone-800"
          />
          {view?.configured ? (
            <span className="mt-1 block text-xs font-normal text-stone-500">
              已保存密钥 {view.api_key_masked}：只改地址/模型时留空即可；更换服务商时请粘贴新
              Key。
            </span>
          ) : null}
        </label>

        <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <ShieldAlert aria-hidden="true" className="mt-0.5 shrink-0" size={18} />
          <label className="leading-6">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              className="mr-2 mt-0.5 size-4 align-text-bottom"
            />
            {meta.confirmText}
          </label>
        </div>

        {error ? <p role="alert" className="rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p> : null}
        {notice ? <p className="rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{notice}</p> : null}

        <div className="flex flex-wrap gap-3">
          <button
            type="submit"
            disabled={saving || clearing || toggling}
            className="focus-ring inline-flex items-center justify-center gap-2 rounded-xl bg-[#1f6857] px-4 py-3 text-sm font-semibold text-white disabled:opacity-60"
          >
            {saving ? <LoaderCircle aria-hidden="true" className="animate-spin" size={16} /> : <Save aria-hidden="true" size={16} />}
            {view?.configured ? "保存修改并启用" : "保存并启用"}
          </button>
          {view?.configured ? (
            confirmingClear ? (
              <>
                <button
                  type="button"
                  onClick={clearProvider}
                  disabled={saving || clearing || toggling}
                  className="focus-ring inline-flex items-center gap-2 rounded-xl bg-red-600 px-4 py-3 text-sm font-semibold text-white disabled:opacity-60"
                >
                  {clearing ? <LoaderCircle aria-hidden="true" className="animate-spin" size={16} /> : <Trash2 aria-hidden="true" size={16} />}
                  确认清除（密钥将被删除）
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmingClear(false)}
                  disabled={saving || clearing || toggling}
                  className="focus-ring inline-flex items-center rounded-xl border border-stone-300 px-4 py-3 text-sm font-semibold text-stone-700 hover:border-stone-400 disabled:opacity-60"
                >
                  取消
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmingClear(true)}
                disabled={saving || clearing || toggling}
                className="focus-ring inline-flex items-center gap-2 rounded-xl border border-stone-300 px-4 py-3 text-sm font-semibold text-stone-700 hover:border-stone-400 disabled:opacity-60"
              >
                <Trash2 aria-hidden="true" size={16} />
                清除配置
              </button>
            )
          ) : null}
        </div>
      </form>
    </section>
  );
}

export function ProviderSettings() {
  const [views, setViews] = useState<Partial<Record<SlotKey, ProviderSettingView>>>({});
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [activeSlot, setActiveSlot] = useState<SlotKey>(initialSlot);

  const applyView = useCallback((slot: SlotKey, view: ProviderSettingView | null) => {
    setViews((previous) => ({ ...previous, [slot]: view }));
  }, []);

  useEffect(() => {
    apiFetch<{ slots: Record<SlotKey, ProviderSettingView> }>("/api/v1/settings/ai-provider")
      .then((payload) => setViews(payload.slots))
      .catch((reason: Error) => {
        setLoadError(reason.message || "读取 Provider 配置失败");
      })
      .finally(() => setLoaded(true));
  }, []);

  function switchSlot(slot: SlotKey) {
    setActiveSlot(slot);
    // 记住所在页签，刷新/分享链接后仍停在同一组配置。
    if (typeof window !== "undefined") {
      window.history.replaceState(null, "", `#${slot}`);
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl">
      <header>
        <h1 className="text-xl font-bold text-stone-900">AI Provider 设置</h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-stone-500">
          为结构化抽取、视觉核验、患者纵向分析、师承写作与 MinerU 云端
          OCR 分别配置 Provider。密钥加密保存于本机数据库，绝不回传页面；修改地址/模型时可不重输密钥；每个
          Provider 可随时停用/启用或清除。点击页签切换分组，圆点表示状态：绿色已启用、琥珀色已停用、灰色未配置。
        </p>
      </header>

      {loadError ? (
        <p role="alert" className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">
          读取 Provider 配置失败：{loadError}。下方显示的不是当前真实配置状态。
        </p>
      ) : null}

      <nav
        aria-label="Provider 分组页签"
        className="sticky top-16 z-20 -mx-1 mt-5 flex flex-wrap gap-2 bg-[#f4f1e9]/95 px-1 py-2 backdrop-blur"
      >
        {SLOT_KEYS.map((slot) => {
          const active = slot === activeSlot;
          return (
            <button
              key={slot}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => switchSlot(slot)}
              className={`focus-ring inline-flex items-center gap-2 rounded-full border px-3.5 py-2 text-sm ${
                active
                  ? "border-[#1f6857] bg-[#1f6857] font-semibold text-white"
                  : "border-stone-300 bg-white text-stone-700 hover:border-stone-400"
              }`}
            >
              <span
                aria-hidden="true"
                className={`size-2 shrink-0 rounded-full ring-1 ring-inset ring-black/10 ${slotDotClass(views[slot])} ${active ? "ring-white/40" : ""}`}
              />
              {TAB_LABEL[slot]}
            </button>
          );
        })}
      </nav>

      {!loaded ? (
        <div className="surface mt-2 h-72 animate-pulse rounded-2xl" aria-hidden="true" />
      ) : (
        <div role="tabpanel" aria-label={TAB_LABEL[activeSlot]}>
          <ProviderCard
            key={activeSlot}
            slot={activeSlot}
            view={views[activeSlot] ?? null}
            onChanged={(view) => applyView(activeSlot, view)}
          />
        </div>
      )}

      <BackupStatusCard />
    </div>
  );
}

type RestoreRun = {
  run_id: string | null;
  status: "passed" | "failed" | "pending";
  started_at: string | null;
  ended_at: string | null;
  last_stage: string | null;
  error_type: string | null;
};

type BackupStatusResponse = {
  configured: boolean;
  directory_accessible: boolean;
  scan_complete: boolean;
  backups: Array<{
    backup_id: string;
    created_at: string | null;
    tier: string | null;
    restore_status: "passed" | "failed" | "pending" | null;
    restore_run: RestoreRun | null;
  }>;
  latest_backup: { backup_id: string; created_at: string; tier: string | null } | null;
  /** 最近尝试：passed / failed / pending（结果待核对）都可能，绝不自动判成败。 */
  latest_restore_attempt: ({ backup_id: string } & RestoreRun) | null;
  /** 最近成功：只有真正演练通过的运行才算。 */
  latest_restore_verification: { backup_id: string; verified_at: string } | null;
};

/** 备份记录、完整性验证、恢复验证是三种证据，分开表达；没有证据时显示
 * "未找到…记录"，不断言"从未验证"，也不合成一个"系统正常"。 */
function BackupStatusCard() {
  const [status, setStatus] = useState<BackupStatusResponse | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let active = true;
    apiFetch<BackupStatusResponse>("/api/v1/backup-status")
      .then((data) => {
        if (active) setStatus(data);
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      })
      .finally(() => {
        if (active) setLoaded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  if (!loaded) return <div className="surface mt-6 h-40 animate-pulse rounded-2xl" aria-hidden="true" />;
  if (error) {
    return (
      <section aria-labelledby="backup-status-title" className="surface mt-6 rounded-2xl p-5 sm:p-6">
        <h2 id="backup-status-title" className="flex items-center gap-2 text-base font-bold text-stone-900"><Archive aria-hidden="true" size={18} className="text-[#1f6857]" />备份状态</h2>
        <p role="alert" className="mt-3 text-sm text-red-700">备份状态读取失败：{error}</p>
      </section>
    );
  }

  const formatTime = (value: string) => new Date(value).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" });
  const attempt = status?.latest_restore_attempt ?? null;

  const attemptDetail = (run: RestoreRun) => {
    if (run.status === "passed") {
      return <>演练通过<span className="ml-2 text-xs text-stone-500">结束于 {run.ended_at ? formatTime(run.ended_at) : "时间未记录"}</span></>;
    }
    if (run.status === "failed") {
      return <>演练未通过<span className="ml-2 text-xs text-rose-700">阶段 {run.last_stage ?? "未记录"} · 错误类型 {run.error_type ?? "未记录"}</span></>;
    }
    return (
      <span className="text-amber-700">
        结果待核对 —— 演练可能仍在进行或已中断，系统不自动判定成败
        <span className="ml-2 text-xs">开始于 {run.started_at ? formatTime(run.started_at) : "时间未记录"}{run.last_stage ? ` · 最后记录阶段 ${run.last_stage}` : ""}</span>
      </span>
    );
  };

  return (
    <section aria-labelledby="backup-status-title" className="surface mt-6 rounded-2xl p-5 sm:p-6">
      <h2 id="backup-status-title" className="flex items-center gap-2 text-base font-bold text-stone-900">
        <Archive aria-hidden="true" size={18} className="text-[#1f6857]" />
        备份状态
      </h2>
      {!status?.configured ? (
        <p className="mt-3 text-sm leading-6 text-stone-500">
          尚未配置备份目录（环境变量 BACKUP_MANIFEST_DIR），系统无法显示备份与恢复验证状态。备份由 <code className="rounded bg-stone-100 px-1">scripts/clinical_backup.py</code> 产生，恢复验证由 <code className="rounded bg-stone-100 px-1">scripts/production_restore_drill.py</code> 产生。
        </p>
      ) : !status.directory_accessible ? (
        <p role="status" className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900">
          已配置备份目录但当前不可访问。Docker 部署需要把备份目录以只读方式挂载给 API 后端，才能在这里显示真实状态。
        </p>
      ) : (
        <>
          {!status.scan_complete ? (
            <p role="status" className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900">
              备份目录扫描不完整（条目过多或读取中断），无法确认哪个备份“最新”；下方列表可能缺少部分备份。
            </p>
          ) : null}
          <dl className="mt-4 grid gap-3 sm:grid-cols-3">
            <div className="rounded-xl border border-stone-200 bg-white p-4">
              <dt className="flex items-center gap-1.5 text-xs font-semibold text-stone-500"><Archive aria-hidden="true" size={13} />最近备份记录</dt>
              <dd className="mt-2 text-sm text-stone-800">
                {status.latest_backup
                  ? <>{formatTime(status.latest_backup.created_at)}<span className="ml-2 font-mono text-xs text-stone-400">{status.latest_backup.backup_id.slice(0, 8)}</span></>
                  : <span className="text-stone-500">{status.scan_complete ? "未找到任何备份记录" : "扫描不完整，无法确认最新"}</span>}
              </dd>
            </div>
            <div className="rounded-xl border border-stone-200 bg-white p-4">
              <dt className="flex items-center gap-1.5 text-xs font-semibold text-stone-500"><History aria-hidden="true" size={13} />最近恢复演练（最近尝试）</dt>
              <dd className="mt-2 text-sm text-stone-800">
                {attempt
                  ? <>{attemptDetail(attempt)}<span className="ml-2 font-mono text-xs text-stone-400">{attempt.backup_id.slice(0, 8)}{attempt.run_id ? ` · 运行 ${attempt.run_id.slice(0, 8)}` : ""}</span></>
                  : <span className="text-stone-500">{status.scan_complete ? "未找到演练运行记录" : "扫描不完整，无法确认最新"}</span>}
              </dd>
            </div>
            <div className="rounded-xl border border-stone-200 bg-white p-4">
              <dt className="flex items-center gap-1.5 text-xs font-semibold text-stone-500"><ShieldCheck aria-hidden="true" size={13} />最近恢复验证成功</dt>
              <dd className="mt-2 text-sm text-stone-800">
                {status.latest_restore_verification
                  ? <>{formatTime(status.latest_restore_verification.verified_at)}<span className="ml-2 font-mono text-xs text-stone-400">{status.latest_restore_verification.backup_id.slice(0, 8)}</span></>
                  : <span className="text-amber-700">{status.scan_complete ? "未找到恢复验证记录" : "扫描不完整，无法确认最新"} —— 备份成功不等于恢复可用，请定期运行恢复演练</span>}
              </dd>
            </div>
          </dl>
          <p className="mt-3 text-xs leading-5 text-stone-500">
            完整性验证：未找到运行记录。备份清单（backup_manifest.json）内含各文件 SHA-256，可据此人工或脚本核对；清单存在本身不算验证成功。
          </p>
          {status.backups.length ? (
            <ul className="mt-4 divide-y divide-stone-100 text-xs text-stone-600">
              {status.backups.slice(0, 5).map((backup) => (
                <li key={backup.backup_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <span className="font-mono text-stone-500">{backup.backup_id.slice(0, 8)}</span>
                  <span>{backup.created_at ? formatTime(backup.created_at) : "时间未记录"}</span>
                  <span>
                    {backup.restore_status === "passed" ? (
                      <span className="rounded bg-emerald-50 px-1.5 py-0.5 font-medium text-emerald-700">恢复已验证</span>
                    ) : backup.restore_status === "failed" ? (
                      <span className="rounded bg-rose-50 px-1.5 py-0.5 font-medium text-rose-700">最近演练未通过</span>
                    ) : backup.restore_status === "pending" ? (
                      <span className="rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-700">结果待核对</span>
                    ) : (
                      <span className="rounded bg-stone-100 px-1.5 py-0.5 text-stone-500">未找到验证记录</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
    </section>
  );
}
