#include "mongoose.h"

#include <stdio.h>
int main(int argc, char ** argv)
{
// 生成一个 32 字符的随机 Token
   char token[65]; // 32个字符 + 结束符 '\0'
   int i = sizeof(token);
   printf("%d\n",i);
   mg_random_str(token, i);
   printf("%.*s\n", i,token);
   printf("%s\n",token);
}
