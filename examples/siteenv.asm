* SITEENV - what this depends on is decided by the ASSEMBLE STEP.
* Run it twice: once plain, once with --sysparm PROD.
SITEENV  CSECT
         STM   14,12,12(13)
&SITE    SETC  '&SYSPARM'
         AIF   ('&SITE' EQ 'PROD').PROD
         COPY  TESTCFG
         L     15,=V(TESTLOG)
         AGO   .DONE
.PROD    ANOP
         COPY  PRODCFG
         L     15,=V(PRODLOG)
.DONE    ANOP
         BALR  14,15
         LM    14,12,12(13)
         BR    14
         END   SITEENV
