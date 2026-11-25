/**
 * 开发期默认测试凭证（仅本地/演示用）。
 *
 * 这是一枚 HS256 JWT：sub=u_dev、user_depts=[fin,corp]、user_roles=[staff]、
 * admin=false、exp=2035-01-01，用仓库 .env 当前的 AUTH_JWT_SECRET 签发，
 * 与 compose 启动 API 时用的密钥一致。
 * 换过 AUTH_JWT_SECRET 时，用 VITE_DEV_TOKEN 注入同签名的 token 覆盖它。
 * 生产环境不要使用：见 api/identity.py 的 mint_dev_token 约束。
 */
const BUILTIN_DEV_TOKEN = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1X2RldiIsInVzZXJfZGVwdHMiOlsiZmluIiwiY29ycCJdLCJ1c2VyX3JvbGVzIjpbInN0YWZmIl0sImFkbWluIjpmYWxzZSwiaWF0IjoxNzk1NTc3MTM3LCJleHAiOjIwNTEyMjI0MDB9.z66unpYSIbtxCaS1QqrU2sNmA5Wu4HvMuaWm8TljFj4';

const configured = import.meta.env.VITE_DEV_TOKEN;

export const DEFAULT_DEV_TOKEN =
  typeof configured === 'string' && configured.trim() ? configured.trim() : BUILTIN_DEV_TOKEN;
