import './globals.css';
import React from 'react';

export const metadata = {
  title: 'Beauty Intelligence PIM Center',
  description: 'Enterprise Beauty Product enrichment and catalog management dashboard.',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div id="root">{children}</div>
      </body>
    </html>
  );
}
