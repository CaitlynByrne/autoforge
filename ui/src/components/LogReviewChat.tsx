/**
 * Log Review Chat Component
 *
 * Full chat interface for AI-powered agent log analysis.
 * Streams a structured analysis then allows follow-up questions.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Send, X, SearchCode, Wifi, WifiOff, RotateCcw } from 'lucide-react'
import { useLogReview } from '../hooks/useLogReview'
import { ChatMessage } from './ChatMessage'
import { TypingIndicator } from './TypingIndicator'
import { isSubmitEnter } from '../lib/keyboard'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent } from '@/components/ui/card'
import { Alert, AlertDescription } from '@/components/ui/alert'

interface LogReviewChatProps {
  projectName: string
  onClose: () => void
}

export function LogReviewChat({
  projectName,
  onClose,
}: LogReviewChatProps) {
  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const handleError = useCallback((err: string) => setError(err), [])

  const {
    messages,
    isLoading,
    isComplete,
    connectionStatus,
    start,
    sendMessage,
    disconnect,
  } = useLogReview({
    projectName,
    onError: handleError,
  })

  // Start the analysis session when component mounts
  useEffect(() => {
    start()
    return () => {
      disconnect()
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  // Focus input when not loading and analysis is complete
  useEffect(() => {
    if (!isLoading && isComplete && inputRef.current) {
      inputRef.current.focus()
    }
  }, [isLoading, isComplete])

  const handleSendMessage = () => {
    const trimmed = input.trim()
    if (!trimmed || isLoading) return

    sendMessage(trimmed)
    setInput('')
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (isSubmitEnter(e)) {
      e.preventDefault()
      handleSendMessage()
    }
  }

  // Connection status indicator
  const ConnectionIndicator = () => {
    switch (connectionStatus) {
      case 'connected':
        return (
          <span className="flex items-center gap-1 text-xs text-green-500">
            <Wifi size={12} />
            Connected
          </span>
        )
      case 'connecting':
        return (
          <span className="flex items-center gap-1 text-xs text-yellow-500">
            <Wifi size={12} className="animate-pulse" />
            Connecting...
          </span>
        )
      case 'error':
        return (
          <span className="flex items-center gap-1 text-xs text-destructive">
            <WifiOff size={12} />
            Error
          </span>
        )
      default:
        return (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <WifiOff size={12} />
            Disconnected
          </span>
        )
    }
  }

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b-2 border-border bg-card">
        <div className="flex items-center gap-3">
          <SearchCode size={20} className="text-primary" />
          <h2 className="font-display font-bold text-lg text-foreground">
            Log Review: {projectName}
          </h2>
          <ConnectionIndicator />
        </div>

        <Button
          onClick={onClose}
          variant="ghost"
          size="icon"
          title="Close"
        >
          <X size={20} />
        </Button>
      </div>

      {/* Error banner */}
      {error && (
        <Alert variant="destructive" className="rounded-none border-x-0 border-t-0">
          <AlertDescription className="flex-1">{error}</AlertDescription>
          <Button
            onClick={() => setError(null)}
            variant="ghost"
            size="icon"
            className="h-6 w-6"
          >
            <X size={14} />
          </Button>
        </Alert>
      )}

      {/* Messages area */}
      <div className="flex-1 overflow-y-auto py-4">
        {messages.length === 0 && !isLoading && (
          <div className="flex flex-col items-center justify-center h-full text-center p-8">
            <Card className="p-6 max-w-md">
              <CardContent className="p-0">
                <h3 className="font-display font-bold text-lg mb-2">
                  Starting Log Analysis
                </h3>
                <p className="text-sm text-muted-foreground">
                  Connecting to Claude to analyze your agent session logs...
                </p>
                {connectionStatus === 'error' && (
                  <Button
                    onClick={start}
                    className="mt-4"
                    size="sm"
                  >
                    <RotateCcw size={14} />
                    Retry Connection
                  </Button>
                )}
              </CardContent>
            </Card>
          </div>
        )}

        {messages.map((message) => (
          <ChatMessage key={message.id} message={message} />
        ))}

        {isLoading && <TypingIndicator />}

        <div ref={messagesEndRef} />
      </div>

      {/* Follow-up input area - only shown after analysis is complete */}
      {isComplete && (
        <div className="p-4 border-t-2 border-border bg-card">
          <div className="flex gap-3">
            <Input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask a follow-up question about the analysis..."
              className="flex-1"
              disabled={isLoading || connectionStatus !== 'connected'}
            />
            <Button
              onClick={handleSendMessage}
              disabled={!input.trim() || isLoading || connectionStatus !== 'connected'}
              className="px-6"
            >
              <Send size={18} />
            </Button>
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            Press Enter to send a follow-up question.
          </p>
        </div>
      )}
    </div>
  )
}
