/**
 * Hook for managing log review WebSocket connection.
 * Simplified version of useExpandChat — no attachments, no feature tracking.
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import type { ChatMessage } from '../lib/types'
import type { LogReviewServerMessage } from '../lib/types'

type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error'

interface UseLogReviewOptions {
  projectName: string
  onError?: (error: string) => void
}

interface UseLogReviewReturn {
  messages: ChatMessage[]
  isLoading: boolean
  isComplete: boolean
  connectionStatus: ConnectionStatus
  start: () => void
  sendMessage: (content: string) => void
  disconnect: () => void
}

function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).substring(2, 9)}`
}

export function useLogReview({
  projectName,
  onError,
}: UseLogReviewOptions): UseLogReviewReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [isComplete, setIsComplete] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('disconnected')

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectAttempts = useRef(0)
  const maxReconnectAttempts = 3
  const pingIntervalRef = useRef<number | null>(null)
  const reconnectTimeoutRef = useRef<number | null>(null)
  const isCompleteRef = useRef(false)
  const manuallyDisconnectedRef = useRef(false)

  useEffect(() => {
    isCompleteRef.current = isComplete
  }, [isComplete])

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (pingIntervalRef.current) {
        clearInterval(pingIntervalRef.current)
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [])

  const connect = useCallback(() => {
    if (manuallyDisconnectedRef.current) return
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    setConnectionStatus('connecting')

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    const wsUrl = `${protocol}//${host}/api/log-review/ws/${encodeURIComponent(projectName)}`

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      setConnectionStatus('connected')
      reconnectAttempts.current = 0
      manuallyDisconnectedRef.current = false

      pingIntervalRef.current = window.setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, 30000)
    }

    ws.onclose = (event) => {
      setConnectionStatus('disconnected')
      if (pingIntervalRef.current) {
        clearInterval(pingIntervalRef.current)
        pingIntervalRef.current = null
      }

      const isAppError = event.code >= 4000 && event.code <= 4999

      if (
        !manuallyDisconnectedRef.current &&
        !isAppError &&
        reconnectAttempts.current < maxReconnectAttempts &&
        !isCompleteRef.current
      ) {
        reconnectAttempts.current++
        const delay = Math.min(1000 * Math.pow(2, reconnectAttempts.current), 10000)
        reconnectTimeoutRef.current = window.setTimeout(connect, delay)
      }
    }

    ws.onerror = () => {
      setConnectionStatus('error')
      onError?.('WebSocket connection error')
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as LogReviewServerMessage

        switch (data.type) {
          case 'text': {
            setMessages((prev) => {
              const lastMessage = prev[prev.length - 1]
              if (lastMessage?.role === 'assistant' && lastMessage.isStreaming) {
                return [
                  ...prev.slice(0, -1),
                  {
                    ...lastMessage,
                    content: lastMessage.content + data.content,
                  },
                ]
              } else {
                return [
                  ...prev,
                  {
                    id: generateId(),
                    role: 'assistant',
                    content: data.content,
                    timestamp: new Date(),
                    isStreaming: true,
                  },
                ]
              }
            })
            break
          }

          case 'analysis_complete': {
            setIsComplete(true)
            // Don't stop loading yet — wait for response_done
            break
          }

          case 'error': {
            setIsLoading(false)
            onError?.(data.content)

            setMessages((prev) => [
              ...prev,
              {
                id: generateId(),
                role: 'system',
                content: `Error: ${data.content}`,
                timestamp: new Date(),
              },
            ])
            break
          }

          case 'pong': {
            break
          }

          case 'response_done': {
            setIsLoading(false)

            setMessages((prev) => {
              const lastMessage = prev[prev.length - 1]
              if (lastMessage?.role === 'assistant' && lastMessage.isStreaming) {
                return [
                  ...prev.slice(0, -1),
                  { ...lastMessage, isStreaming: false },
                ]
              }
              return prev
            })
            break
          }
        }
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e)
      }
    }
  }, [projectName, onError])

  const start = useCallback(() => {
    connect()

    let attempts = 0
    const maxAttempts = 50
    const checkAndSend = () => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        setIsLoading(true)
        wsRef.current.send(JSON.stringify({ type: 'start' }))
      } else if (wsRef.current?.readyState === WebSocket.CONNECTING) {
        if (attempts++ < maxAttempts) {
          setTimeout(checkAndSend, 100)
        } else {
          onError?.('Connection timeout')
          setIsLoading(false)
        }
      }
    }

    setTimeout(checkAndSend, 100)
  }, [connect, onError])

  const sendMessage = useCallback((content: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      onError?.('Not connected')
      return
    }

    setMessages((prev) => [
      ...prev,
      {
        id: generateId(),
        role: 'user',
        content,
        timestamp: new Date(),
      },
    ])

    setIsLoading(true)
    wsRef.current.send(JSON.stringify({ type: 'message', content }))
  }, [onError])

  const disconnect = useCallback(() => {
    manuallyDisconnectedRef.current = true
    reconnectAttempts.current = maxReconnectAttempts
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current)
      pingIntervalRef.current = null
    }
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current)
      reconnectTimeoutRef.current = null
    }
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setConnectionStatus('disconnected')
  }, [])

  return {
    messages,
    isLoading,
    isComplete,
    connectionStatus,
    start,
    sendMessage,
    disconnect,
  }
}
