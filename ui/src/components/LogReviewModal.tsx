/**
 * Log Review Modal
 *
 * Full-screen modal wrapper for the LogReviewChat component.
 * Allows users to get AI analysis of agent session logs.
 */

import { LogReviewChat } from './LogReviewChat'

interface LogReviewModalProps {
  isOpen: boolean
  projectName: string
  onClose: () => void
}

export function LogReviewModal({
  isOpen,
  projectName,
  onClose,
}: LogReviewModalProps) {
  if (!isOpen) return null

  return (
    <div className="fixed inset-0 z-50 bg-background">
      <LogReviewChat
        projectName={projectName}
        onClose={onClose}
      />
    </div>
  )
}
