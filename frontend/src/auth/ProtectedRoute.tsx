import { Navigate, useLocation } from 'react-router-dom'
import type { ReactElement } from 'react'
import { useAuth } from './AuthContext'

export default function ProtectedRoute({ children }: { children: ReactElement }) {
  const { token } = useAuth()
  const location = useLocation()
  if (!token) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return children
}
