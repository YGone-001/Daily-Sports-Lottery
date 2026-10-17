/**
 * API 基础客户端
 * 封装通用 Fetch 请求、查询参数拼接与统一错误处理
 */

export class ApiClientError extends Error {
  readonly status: number
  readonly statusText: string
  readonly data?: unknown

  constructor(message: string, status: number, statusText: string, data?: unknown) {
    super(message)
    this.name = 'ApiClientError'
    this.status = status
    this.statusText = statusText
    this.data = data
  }
}

export type QueryParams = Record<
  string,
  string | number | boolean | null | undefined
>

/**
 * 将参数对象转换为 URL 查询字符串（过滤 undefined 与 null）
 */
export function buildQueryString(params?: QueryParams): string {
  if (!params) return ''
  const searchParams = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      searchParams.append(key, String(value))
    }
  }
  const qs = searchParams.toString()
  return qs ? `?${qs}` : ''
}

export interface RequestOptions extends RequestInit {
  params?: QueryParams
}

/**
 * 底层通用 HTTP 请求方法
 */
export async function request<T>(
  url: string,
  options: RequestOptions = {}
): Promise<T> {
  const { params, headers, ...restOptions } = options
  const fullUrl = `${url}${buildQueryString(params)}`

  const requestHeaders = new Headers(headers)
  if (!requestHeaders.has('Accept')) {
    requestHeaders.set('Accept', 'application/json')
  }

  let response: Response
  try {
    response = await fetch(fullUrl, {
      headers: requestHeaders,
      ...restOptions,
    })
  } catch (networkError) {
    throw new ApiClientError(
      networkError instanceof Error ? networkError.message : '网络请求失败，请检查网络连接',
      0,
      'NETWORK_ERROR'
    )
  }

  let data: unknown = null
  const contentType = response.headers.get('content-type')
  if (contentType && contentType.includes('application/json')) {
    try {
      data = await response.json()
    } catch {
      data = null
    }
  } else {
    try {
      const text = await response.text()
      data = text || null
    } catch {
      data = null
    }
  }

  if (!response.ok) {
    let errorMessage = `请求失败 (${response.status} ${response.statusText})`
    if (data && typeof data === 'object') {
      const errorObj = data as Record<string, unknown>
      if (typeof errorObj['error'] === 'string') {
        errorMessage = errorObj['error']
      } else if (typeof errorObj['message'] === 'string') {
        errorMessage = errorObj['message']
      }
    }
    throw new ApiClientError(errorMessage, response.status, response.statusText, data)
  }

  return data as T
}

/**
 * 封装 GET / POST / PUT / DELETE 常用方法
 */
export const apiClient = {
  get<T>(url: string, params?: QueryParams, init?: RequestInit): Promise<T> {
    return request<T>(url, {
      method: 'GET',
      params,
      ...init,
    })
  },

  post<T>(url: string, body?: unknown, init?: RequestInit): Promise<T> {
    const headers = new Headers(init?.headers)
    let bodyPayload: BodyInit | undefined

    if (body !== undefined) {
      if (!headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json')
      }
      bodyPayload = JSON.stringify(body)
    }

    return request<T>(url, {
      method: 'POST',
      body: bodyPayload,
      headers,
      ...init,
    })
  },
}
