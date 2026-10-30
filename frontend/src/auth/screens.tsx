export function LoadingScreen() {
  return (
    <main className="auth-screen">
      <div className="auth-card" role="status">
        <h1>Checking your session…</h1>
      </div>
    </main>
  );
}

export function ConfigurationError({ problems }: { problems: string[] }) {
  return (
    <main className="auth-screen">
      <div className="auth-card" role="alert">
        <h1>Authentication is not configured</h1>
        <p>
          The dashboard cannot sign you in until the Cognito settings are
          provided. There is no way to use it without signing in.
        </p>
        <ul>
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
        <p>See the README section &ldquo;Frontend sign-in&rdquo; for the variables.</p>
      </div>
    </main>
  );
}

export function SignInScreen({ notice, onSignIn }: { notice?: string; onSignIn: () => void }) {
  return (
    <main className="auth-screen">
      <div className="auth-card">
        <h1>Irish Energy Analytics</h1>
        <p>Sign in to view demand analytics and pipeline run history.</p>
        {notice && (
          <p className="auth-notice" role="alert">
            {notice}
          </p>
        )}
        <button className="primary-button" onClick={onSignIn}>
          Sign in
        </button>
      </div>
    </main>
  );
}
