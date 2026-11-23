// PDF.js 接线：加载、渲染、错误归一。
//
// 为什么自己接一层：`getDocument` 的失败有三种完全不同的原因（样本缺失 404、
// 无权限 403、服务下线 503），而 pdf.js 抛的是它自己的异常类型。界面要按
// §11.3 给出不同文案，所以必须在边界上把异常翻成 `UiError`，不能让 pdf.js 的
// 异常对象漏到组件里（那里面带的是内部 URL 与解析细节）。

import * as pdfjsLib from 'pdfjs-dist';
import type { PDFDocumentProxy } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { buildError } from './api';
import type { UiError } from './types';

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

export interface OpenPdfOptions {
  url: string;
  token: string;
  signal?: AbortSignal;
}

export type OpenPdfResult =
  | { ok: true; doc: PDFDocumentProxy }
  | { ok: false; error: UiError };

/** 从 pdf.js 的异常里只取 HTTP 状态码；其余一律当协议错误。 */
export function statusFromPdfError(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const status = (error as { status?: unknown }).status;
  return typeof status === 'number' ? status : undefined;
}

export async function openPdf({ url, token, signal }: OpenPdfOptions): Promise<OpenPdfResult> {
  const task = pdfjsLib.getDocument({
    url,
    // 样本接口要 Bearer 凭证（附录 C.1 + §11.1 闸二）。
    httpHeaders: { Authorization: `Bearer ${token}` },
    withCredentials: false,
    // 不在前端拼「分片下载」：样本都是演示体量，整份下载最省事、也不会和 Range 语义打架。
    disableRange: true,
    disableStream: true,
    // 非嵌入的标准字体（Helvetica 等）要 pdf.js 自己去取 .pfb，否则整页渲染成空白
    // ——实测：`samples/policy/travel-cn.pdf` 的 canvas 非白像素数为 0，而页面结构、
    // 行高都对，看起来像"渲染成功"。字体文件随仓库放在 public/pdfjs/ 下。
    standardFontDataUrl: `${import.meta.env.BASE_URL}pdfjs/standard_fonts/`,
    // 系统字体作为兜底：标准字体文件取不到时（离线部署没拷 public/pdfjs/），
    // 非嵌入字体仍能画出字形，而不是整页空白。
    useSystemFonts: true,
  });
  const onAbort = () => {
    void task.destroy();
  };
  signal?.addEventListener('abort', onAbort, { once: true });
  try {
    const doc = await task.promise;
    return { ok: true, doc };
  } catch (error) {
    const status = statusFromPdfError(error);
    if (status) return { ok: false, error: buildError(status, {}) };
    if ((error as { name?: string }).name === 'AbortError') {
      return { ok: false, error: { kind: 'interrupted', message: '已中断，可重试' } };
    }
    // pdf.js 的 MissingPDFException / InvalidPDFException：库里有行但文件没了/坏了。
    return { ok: false, error: { kind: 'not_found', message: '未找到该文档或样本文件缺失' } };
  } finally {
    signal?.removeEventListener('abort', onAbort);
  }
}

/**
 * 渲染一页到 canvas。`cssWidth` 是容器可用宽度：按它算缩放，再乘以 devicePixelRatio
 * 渲染，截图和投影上都清晰。返回值是页面在 CSS 像素下的宽高（覆盖层不需要它，
 * 因为高亮用的是百分比坐标——见 bbox.ts）。
 *
 * **同一个 canvas 上不能有两个 render() 同时进行**。pdf.js 会直接抛
 * "Cannot use the same canvas during multiple render() operations"，而且抛的时机在
 * 第二个任务上——第一个任务被丢在中间状态，最终表现是**页面一片白、却没有任何报错**
 * （实测：翻页比上一次渲染快时必现）。所以这里接受 `signal`，取消时主动
 * `task.cancel()`，把"新渲染先取消旧的"这件事做在渲染函数内部，而不是指望调用方记得。
 */
export async function renderPageToCanvas(
  doc: PDFDocumentProxy,
  pageNum: number,
  canvas: HTMLCanvasElement,
  cssWidth: number,
  options: { signal?: AbortSignal } = {},
): Promise<{ cssWidth: number; cssHeight: number }> {
  const page = await doc.getPage(pageNum);
  const base = page.getViewport({ scale: 1 });
  const scale = cssWidth / base.width;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const viewport = page.getViewport({ scale: scale * ratio });
  const context = canvas.getContext('2d');
  if (!context) throw new Error('canvas 2d context 不可用');
  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  canvas.style.width = '100%';
  canvas.style.height = 'auto';

  const task = page.render({ canvasContext: context, viewport });
  const onAbort = () => task.cancel();
  options.signal?.addEventListener('abort', onAbort, { once: true });
  try {
    await task.promise;
  } finally {
    options.signal?.removeEventListener('abort', onAbort);
  }
  return { cssWidth, cssHeight: base.height * scale };
}
