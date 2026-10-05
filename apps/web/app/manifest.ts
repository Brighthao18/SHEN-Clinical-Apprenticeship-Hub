import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "师承临床知识库",
    short_name: "师承知识库",
    description: "个人中医师承临床知识管理系统",
    start_url: "/",
    display: "standalone",
    background_color: "#f4f1e9",
    theme_color: "#1f6857",
    icons: [
      {
        src: "/icon.svg",
        sizes: "any",
        type: "image/svg+xml",
        purpose: "any",
      },
    ],
  };
}
