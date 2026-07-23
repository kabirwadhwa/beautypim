"use client";

import React, { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowRight, Bot, CheckCircle2, MessageCircle, Search, Send,
  ShieldCheck, Sparkles, User
} from "lucide-react";
import Shell from "../../components/Shell";
import { API_URL } from "../../config";
import styles from "../page.module.css";

interface ProductMatch {
  id: string;
  internal_code: string;
  name: string;
  brand: string;
  category: string | null;
  description: string | null;
  review_status: string;
  match_reasons: string[];
  matched_attributes: Record<string, unknown>;
}

interface AssistantResponse {
  answer: string;
  products: ProductMatch[];
  total_matches: number;
  interpreted_filters: Record<string, unknown>;
  provider: string;
}

interface ChatEntry {
  role: "user" | "assistant";
  content: string;
  response?: AssistantResponse;
}

const suggestions = [
  "Show me skincare products",
  "Find body oils without retinol",
  "Which products target hydration?",
  "Show vegan products for sensitive skin",
];

export default function CatalogueAssistantPage() {
  const router = useRouter();
  const [message, setMessage] = useState("");
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ask = async (question: string) => {
    const cleanQuestion = question.trim();
    if (!cleanQuestion || loading) return;

    const priorChat = chat;
    setChat([...priorChat, { role: "user", content: cleanQuestion }]);
    setMessage("");
    setLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem("token");
      const response = await fetch(`${API_URL}/assistant/chat`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          message: cleanQuestion,
          history: priorChat.slice(-8).map(item => ({
            role: item.role,
            content: item.content,
          })),
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || "The catalogue assistant could not answer.");
      }
      setChat(current => [
        ...current,
        { role: "assistant", content: data.answer, response: data },
      ]);
    } catch (caught: any) {
      setError(caught.message || "The catalogue assistant could not answer.");
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    ask(message);
  };

  return (
    <Shell>
      <div className={styles.pageHeader} style={{ alignItems: "flex-start" }}>
        <div className={styles.titleGroup}>
          <h1 style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Sparkles size={28} color="#818cf8" />
            AI Catalogue Assistant
          </h1>
          <p>Ask naturally. Every answer is grounded in products stored in Beauty PIM.</p>
        </div>
        <div className={styles.badge} style={{ color: "#34d399", border: "1px solid #10b98155" }}>
          <ShieldCheck size={13} /> Catalogue-grounded
        </div>
      </div>

      <div style={{
        display: "grid", gridTemplateColumns: "minmax(0, 3fr) minmax(280px, 1fr)",
        gap: 20, alignItems: "start"
      }}>
        <section className={styles.panelCard} style={{ minHeight: 650, padding: 0, overflow: "hidden" }}>
          <div style={{
            minHeight: 530, maxHeight: "calc(100vh - 300px)", overflowY: "auto",
            padding: 24, display: "flex", flexDirection: "column", gap: 18
          }}>
            {chat.length === 0 && (
              <div style={{
                flex: 1, display: "flex", flexDirection: "column", alignItems: "center",
                justifyContent: "center", textAlign: "center", padding: "50px 20px"
              }}>
                <div style={{
                  width: 64, height: 64, borderRadius: 18, display: "grid", placeItems: "center",
                  background: "linear-gradient(135deg, #4f46e5, #7c3aed)", marginBottom: 18
                }}>
                  <Bot size={32} color="white" />
                </div>
                <h2 style={{ fontSize: 22, marginBottom: 8 }}>What are you looking for?</h2>
                <p style={{ color: "#94a3b8", maxWidth: 560, lineHeight: 1.6, marginBottom: 24 }}>
                  Search by category, product type, brand, ingredient, concern, claim or review state.
                  I only return products that exist in your live catalogue.
                </p>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 10, justifyContent: "center" }}>
                  {suggestions.map(suggestion => (
                    <button
                      key={suggestion}
                      onClick={() => ask(suggestion)}
                      className={styles.btnSecondary}
                      style={{ fontSize: 12 }}
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {chat.map((entry, index) => (
              <div key={index} style={{
                display: "flex", gap: 10,
                flexDirection: entry.role === "user" ? "row-reverse" : "row",
              }}>
                <div style={{
                  flex: "0 0 34px", width: 34, height: 34, borderRadius: 10,
                  display: "grid", placeItems: "center",
                  background: entry.role === "user" ? "#1e293b" : "#4f46e5",
                }}>
                  {entry.role === "user" ? <User size={17} /> : <Bot size={17} />}
                </div>
                <div style={{ maxWidth: entry.role === "user" ? "75%" : "calc(100% - 44px)", width: entry.role === "assistant" ? "100%" : "auto" }}>
                  <div style={{
                    padding: "11px 14px", borderRadius: 10, fontSize: 13, lineHeight: 1.55,
                    background: entry.role === "user" ? "#25304d" : "#111a30",
                    border: "1px solid #2e3c64",
                  }}>
                    {entry.content}
                  </div>

                  {entry.response && (
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 12, marginTop: 12 }}>
                      {entry.response.products.map(product => (
                        <article key={product.id} style={{
                          padding: 15, borderRadius: 9, border: "1px solid #33466f",
                          background: "linear-gradient(145deg, #121c33, #0d1426)",
                          display: "flex", flexDirection: "column", gap: 9,
                        }}>
                          <div style={{ display: "flex", justifyContent: "space-between", gap: 10 }}>
                            <div>
                              <div style={{ color: "#a5b4fc", fontSize: 10, marginBottom: 3 }}>{product.internal_code}</div>
                              <h3 style={{ fontSize: 15, color: "#f8fafc", margin: 0 }}>{product.name}</h3>
                              <div style={{ color: "#94a3b8", fontSize: 12, marginTop: 3 }}>{product.brand}</div>
                            </div>
                            <span className={styles.badge} style={{ height: "fit-content", fontSize: 9 }}>
                              {product.review_status}
                            </span>
                          </div>
                          {product.category && (
                            <div style={{ color: "#c4b5fd", fontSize: 11 }}>{product.category}</div>
                          )}
                          <p style={{
                            color: "#cbd5e1", fontSize: 12, lineHeight: 1.5, margin: 0,
                            display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical",
                            overflow: "hidden",
                          }}>
                            {product.description || "No source description is available."}
                          </p>
                          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                            {product.match_reasons.map(reason => (
                              <div key={reason} style={{ display: "flex", gap: 5, color: "#86efac", fontSize: 10 }}>
                                <CheckCircle2 size={12} /> {reason}
                              </div>
                            ))}
                          </div>
                          <button
                            onClick={() => router.push(`/products/${product.id}`)}
                            className={styles.btnSecondary}
                            style={{ marginTop: "auto", justifyContent: "space-between", width: "100%" }}
                          >
                            Inspect product <ArrowRight size={14} />
                          </button>
                        </article>
                      ))}
                    </div>
                  )}
                  {entry.response && (
                    <div style={{ color: "#64748b", fontSize: 9, marginTop: 7 }}>
                      Search interpretation: {entry.response.provider}
                    </div>
                  )}
                </div>
              </div>
            ))}

            {loading && (
              <div style={{ display: "flex", gap: 10, alignItems: "center", color: "#94a3b8", fontSize: 12 }}>
                <div style={{
                  width: 34, height: 34, borderRadius: 10, display: "grid", placeItems: "center",
                  background: "#4f46e5"
                }}>
                  <Bot size={17} />
                </div>
                <Sparkles size={15} color="#818cf8" /> Searching the live catalogue…
              </div>
            )}
          </div>

          <form onSubmit={handleSubmit} style={{
            display: "flex", gap: 10, padding: 16, borderTop: "1px solid #2e3c64",
            background: "#0e162a"
          }}>
            <div style={{ position: "relative", flex: 1 }}>
              <Search size={17} style={{ position: "absolute", left: 13, top: 13, color: "#64748b" }} />
              <input
                value={message}
                onChange={event => setMessage(event.target.value)}
                placeholder="Ask: Show me skincare products for sensitive skin…"
                disabled={loading}
                className={styles.inputField}
                style={{ width: "100%", paddingLeft: 40, height: 43 }}
              />
            </div>
            <button
              type="submit"
              disabled={loading || !message.trim()}
              className={`${styles.btn} ${styles.btnPrimary}`}
              style={{ opacity: loading || !message.trim() ? 0.5 : 1 }}
            >
              <Send size={16} /> Ask
            </button>
          </form>
          {error && (
            <div style={{ color: "#f87171", fontSize: 12, padding: "0 16px 14px" }}>{error}</div>
          )}
        </section>

        <aside>
          <div className={styles.panelCard}>
            <div className={styles.panelTitle}>
              <MessageCircle size={17} color="#818cf8" /> What you can ask
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 12, color: "#94a3b8", fontSize: 12, lineHeight: 1.5 }}>
              <div><strong style={{ color: "#e2e8f0" }}>Discover</strong><br />Categories, brands and product types</div>
              <div><strong style={{ color: "#e2e8f0" }}>Qualify</strong><br />Ingredients, claims and concerns</div>
              <div><strong style={{ color: "#e2e8f0" }}>Govern</strong><br />Review and publication status</div>
              <div><strong style={{ color: "#e2e8f0" }}>Refine</strong><br />Ask follow-up questions naturally</div>
            </div>
          </div>
          <div className={styles.panelCard} style={{ background: "rgba(16,185,129,0.05)", borderColor: "#10b98133" }}>
            <div style={{ display: "flex", gap: 8, color: "#6ee7b7", fontWeight: 600, fontSize: 13, marginBottom: 8 }}>
              <ShieldCheck size={17} /> No invented products
            </div>
            <p style={{ color: "#94a3b8", fontSize: 11, lineHeight: 1.55, margin: 0 }}>
              AI interprets your request. Matching and product details come from the Beauty PIM database.
            </p>
          </div>
        </aside>
      </div>
    </Shell>
  );
}
