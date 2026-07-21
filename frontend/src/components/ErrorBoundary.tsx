import { Component, type ErrorInfo, type ReactNode } from 'react'
import { reportIssue } from '../api/client'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error('Unhandled UI error:', error, info.componentStack)
    reportIssue('error-boundary', `${error.name}: ${error.message}`, {
      path: window.location.pathname,
      stack: (error.stack ?? '').slice(0, 4000),
      componentStack: (info.componentStack ?? '').slice(0, 2000)
    })
  }

  render() {
    if (this.state.error) {
      return (
        <div className="error-box" role="alert">
          <strong>Something went wrong rendering this page.</strong>
          <div className="muted small">{this.state.error.message}</div>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => this.setState({ error: null })}
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
